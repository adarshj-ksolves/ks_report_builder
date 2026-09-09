import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

# Column types that can be selected.
KS_SCALAR_TYPES = (
    'char', 'text', 'selection', 'integer', 'float', 'monetary',
    'boolean', 'date', 'datetime', 'many2one',
)
# Only many2one may be traversed: crossing a one2many/many2many fans out rows
# and every measure would silently double count. Restricting the picker's own
# JS filter to KS_SCALAR_TYPES (ks_field_chain_field.js) is what actually
# enforces this - a one2many/many2many never even appears as a selectable row,
# so it can never be chosen as a mid-path hop either. This constant is kept
# here too so server-side validation (_ks_walk_path) matches the client
# exactly, in case a path is set via RPC rather than through the widget.
KS_TRAVERSABLE_TYPES = ('many2one',)

# x2many relations are NOT added to KS_TRAVERSABLE_TYPES on purpose: that
# constant governs the JOIN builder, and joining a list really would fan out
# rows (Invariant #3, unchanged). They are instead crossed by collapsing the
# whole list to ONE scalar inside a correlated subquery - the same mechanism
# kind='aggregate' already uses - so the outer query still returns exactly one
# row per base record. See ks_split_path / _ks_build_flatten_subquery.
KS_X2MANY_TYPES = ('one2many', 'many2many')

# monetary needs a currency_field companion and selection needs
# ir.model.fields.selection rows, neither of which a generated column can
# supply reliably, so both degrade to a simpler type.
KS_TTYPE_OVERRIDE = {'monetary': 'float', 'selection': 'char'}


def ks_slugify(value):
    return re.sub(r'[^a-z0-9]+', '_', (value or '').lower()).strip('_')


def ks_walk_path(env, model_name, path):
    """Walk a dotted field path against ``model_name``, returning the final
    Field. Shared by the mixin's own ``path`` (relative to ``base_model_id``)
    and by KsReportBuilderField's aggregate-column correlation key (relative
    to the same base model, but stored on a separate field) - anywhere a
    dotted path needs the exact same many2one-only, scalar-terminal
    validation.

    Raises ValidationError on an unknown segment, on drilling through
    anything other than a many2one, or if the final field's type isn't one
    this module knows how to turn into a column.
    """
    if not path:
        raise ValidationError(_("Pick the source field."))
    segments = path.split('.')
    field = None
    for index, segment in enumerate(segments):
        model = env.get(model_name)
        if model is None:
            raise ValidationError(_("Model %s does not exist.", model_name))
        field = model._fields.get(segment)
        if field is None:
            raise ValidationError(_(
                "Field %(f)s does not exist on %(m)s.", f=segment, m=model_name))
        if index == len(segments) - 1:
            break
        if field.type not in KS_TRAVERSABLE_TYPES:
            raise ValidationError(_(
                "%(f)s is a %(t)s and cannot be drilled into. Only "
                "many2one relations can be followed, otherwise totals "
                "would be counted more than once.",
                f=segment, t=field.type))
        model_name = field.comodel_name
    if field.type not in KS_SCALAR_TYPES:
        raise ValidationError(_(
            "%(f)s is a %(t)s and cannot be used as a column.",
            f=segments[-1], t=field.type))
    return field


def ks_split_path(env, model_name, path):
    """Walk a dotted path that may cross AT MOST ONE x2many hop.

    ``ks_walk_path`` above stays strictly many2one-only (it backs snapshot
    fields and the aggregate correlation key, where a list makes no sense).
    This is its superset, used for report columns: it additionally allows one
    one2many/many2many hop, which the SQL builder then collapses to a single
    value with a correlated subquery rather than a join.

    Returns a dict:
      ``prefix``       many2one path from the base model to the model that
                       OWNS the list ('' when the list is on the base model)
      ``owner_model``  technical name of that owning model
      ``x2many``       the x2many ``Field``, or None for a plain m2o path
      ``suffix``       path INSIDE the child model, from the list down to the
                       final scalar ('' when the list itself is the last
                       segment - only a Count collapse is meaningful then)
      ``field``        the terminal scalar ``Field``, or None when the list
                       itself is the last segment
    """
    if not path:
        raise ValidationError(_("Pick the source field."))
    segments = path.split('.')
    x2many = None
    x2many_index = None
    owner_model = model_name
    current = model_name
    final_field = None

    for index, segment in enumerate(segments):
        model = env.get(current)
        if model is None:
            raise ValidationError(_("Model %s does not exist.", current))
        field = model._fields.get(segment)
        if field is None:
            raise ValidationError(_(
                "Field %(f)s does not exist on %(m)s.", f=segment, m=current))
        is_last = index == len(segments) - 1

        if field.type in KS_X2MANY_TYPES:
            if x2many is not None:
                raise ValidationError(_(
                    "%(p)s goes through more than one list. Only one "
                    "one2many/many2many can be flattened per column - after "
                    "the first list, only single relations can be followed.",
                    p=path))
            x2many, x2many_index, owner_model = field, index, current
            current = field.comodel_name
            continue

        if is_last:
            final_field = field
            break

        if field.type not in KS_TRAVERSABLE_TYPES:
            raise ValidationError(_(
                "%(f)s is a %(t)s and cannot be drilled into.",
                f=segment, t=field.type))
        current = field.comodel_name

    if final_field is not None:
        if final_field.type not in KS_SCALAR_TYPES:
            raise ValidationError(_(
                "%(f)s is a %(t)s and cannot be used as a column.",
                f=segments[-1], t=final_field.type))

    return {
        'prefix': '.'.join(segments[:x2many_index]) if x2many_index is not None else '',
        'owner_model': owner_model,
        'x2many': x2many,
        'suffix': '.'.join(segments[x2many_index + 1:]) if x2many_index is not None else '',
        'field': final_field,
    }


def ks_x2many_sql_info(env, field, owner_model_name):
    """Validate that ``field`` (a one2many/many2many) is backed by real tables
    and return everything needed to correlate a subquery back to its owner.

    Verified against the Odoo 19 source (odoo/orm/fields_relational.py):
    - a One2many's ``inverse_name`` is NOT guaranteed to be a stored many2one:
      it may be absent entirely, non-stored/computed (`:1171-1187` falls back
      to Python), or a ``many2one_reference`` generic FK (`:952`).
    - a Many2many's ``relation``/``column1``/``column2`` are filled lazily in
      ``setup_nonrelated`` and are explicitly set to None when the field is
      not stored (`:1292`), so all three must be checked.
    - ``get_comodel_domain()`` (`:99-110`) returns the field's own ``domain=``
      AND, for One2many, ``_additional_domain`` (`:906-912`) which adds the
      ``res_model``-style discriminator for a generic FK. Applying it is NOT
      optional: without it, a one2many over a model keyed by res_model/res_id
      (mail.message, ir.attachment) silently matches OTHER models' rows that
      happen to share the same numeric id.
    """
    if not field.store:
        raise ValidationError(_(
            "%s is not stored, so it has no rows to flatten.", field.name))
    comodel = env.get(field.comodel_name)
    if comodel is None or not comodel._auto:
        raise ValidationError(_(
            "%(f)s points at %(m)s, which is not backed by a real table.",
            f=field.name, m=field.comodel_name))

    info = {
        'child_model': field.comodel_name,
        'child_table': comodel._table,
        # The field's own domain= plus (for o2m) the generic-FK discriminator.
        'domain': field.get_comodel_domain(env[owner_model_name]),
    }
    if field.type == 'one2many':
        if not field.inverse_name:
            raise ValidationError(_(
                "%s has no inverse field, so there is no column to match "
                "child rows on.", field.name))
        inverse = comodel._fields.get(field.inverse_name)
        if inverse is None or not inverse.store:
            raise ValidationError(_(
                "The inverse field of %s is not stored, so there is no "
                "column to match child rows on.", field.name))
        if inverse.type not in ('many2one', 'many2one_reference'):
            raise ValidationError(_(
                "The inverse field of %(f)s is a %(t)s, which is not "
                "supported.", f=field.name, t=inverse.type))
        info.update(mode='one2many', inverse_column=inverse.name)
    else:
        if not (field.relation and field.column1 and field.column2):
            raise ValidationError(_(
                "%s has no relation table, so its rows cannot be matched.",
                field.name))
        info.update(
            mode='many2many',
            rel_table=field.relation,
            rel_owner_column=field.column1,
            rel_child_column=field.column2,
        )
    return info


class KsFieldPathMixin(models.AbstractModel):
    """A dotted field path, typed directly by an arbitrary-depth field-chain
    picker widget (``ks_field_chain_picker``, wrapping Odoo's own
    ``ModelFieldSelector``) rather than a fixed number of chained dropdowns.

    Concrete models supply ``base_model_id``.
    """
    _name = 'ks.field.path.mixin'
    _description = 'Field Path Picker'

    base_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Source Model',
        ondelete='cascade',
    )
    base_model_technical_name = fields.Char(
        related='base_model_id.model',
        string='Source Model Technical Name',
        help="Used to point the field-chain picker widget at the source "
             "model - it needs the technical name as a string, not the "
             "base_model_id many2one value.",
    )
    path = fields.Char(string='Field Path')
    ttype = fields.Char(string='Type', compute='_compute_ttype_relation', store=True)
    relation = fields.Char(string='Relation', compute='_compute_ttype_relation', store=True)

    def _ks_path_active(self):
        """Concrete models may disable path resolution for some records."""
        self.ensure_one()
        return True

    def _ks_walk_path(self):
        """Walk ``path`` against ``base_model_id``, returning the final Field."""
        self.ensure_one()
        if not self.base_model_id:
            raise ValidationError(_("Pick the source model first."))
        return ks_walk_path(self.env, self.base_model_id.model, self.path)

    @api.depends('path', 'base_model_id')
    def _compute_ttype_relation(self):
        for line in self:
            if not line._ks_path_active() or not line.path or not line.base_model_id:
                line.ttype = 'float'
                line.relation = False
                continue
            try:
                field = line._ks_walk_path()
            except ValidationError:
                # Leave a transiently invalid path (e.g. mid-edit via RPC)
                # with a harmless default; _check_path enforces validity for
                # real at save time.
                line.ttype = 'float'
                line.relation = False
                continue
            if field is None:
                # A subclass whose _ks_walk_path allows a path ending on the
                # list itself (flatten + Count) has no terminal field to type
                # from; it overrides _compute_ttype_relation to set the real
                # type after this super() call.
                line.ttype = 'float'
                line.relation = False
                continue
            raw_type = field.type
            line.ttype = KS_TTYPE_OVERRIDE.get(raw_type, raw_type)
            line.relation = field.comodel_name if raw_type == 'many2one' else False

    @api.constrains('path', 'base_model_id')
    def _check_path(self):
        for line in self:
            if line._ks_path_active() and line.path and line.base_model_id:
                line._ks_walk_path()
