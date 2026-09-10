import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

# Column types that can be selected.
KS_SCALAR_TYPES = (
    'char', 'text', 'selection', 'integer', 'float', 'monetary',
    'boolean', 'date', 'datetime', 'many2one',
)
# Only many2one may be traversed: crossing a list in a JOIN fans out rows and
# every measure would double count. Mirrored by the picker's JS filter; kept
# server-side too, for paths set via RPC rather than the widget.
KS_TRAVERSABLE_TYPES = ('many2one',)

# x2many relations stay out of KS_TRAVERSABLE_TYPES on purpose: that constant
# governs the JOIN builder. They are crossed instead by collapsing the list to
# one scalar inside a correlated subquery, so the outer query still returns one
# row per base record. See ks_split_path / _ks_build_flatten_subquery.
KS_X2MANY_TYPES = ('one2many', 'many2many')

# monetary needs a currency_field companion and selection needs
# ir.model.fields.selection rows, neither of which a generated column can
# supply reliably, so both degrade to a simpler type.
KS_TTYPE_OVERRIDE = {'monetary': 'float', 'selection': 'char'}


def ks_slugify(value):
    return re.sub(r'[^a-z0-9]+', '_', (value or '').lower()).strip('_')


def KS_COMPANY_DEPENDENT_ERROR(field_name):
    """Shared rejection message for company_dependent ("Per Company") fields.

    These are stored as jsonb keyed by company id, so joining one as a plain
    integer FK raises `operator does not exist: integer = jsonb`. Checked on
    every path segment, not only the leaf. Reading one requires knowing WHICH
    company, which a query compiled once at deploy time cannot resolve per
    request - hence the explicit per-report property_company_id opt-in.
    """
    return _(
        "%(f)s is a \"Per Company\" field - its stored value depends on "
        "which company is looking, which a query saved once cannot express "
        "on its own. If this report has a 'Report Company' set, %(f)s can "
        "be used (its value for that one company); otherwise it is "
        "rejected rather than silently pick a company for you.",
        f=field_name)


def ks_walk_path(env, model_name, path, allow_company_dependent=False):
    """Walk a dotted field path against ``model_name``, returning the final
    Field. many2one-only traversal, scalar terminal.

    ``allow_company_dependent`` is ``bool(report.property_company_id)`` for
    report columns; snapshot fields leave it False and always reject.

    Raises ValidationError on an unknown segment, on drilling through anything
    other than a many2one, or on an unsupported terminal type.
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
        if getattr(field, 'company_dependent', False) and not allow_company_dependent:
            raise ValidationError(KS_COMPANY_DEPENDENT_ERROR(segment))
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


def ks_split_path(env, model_name, path, allow_company_dependent=False):
    """Walk a dotted path that may cross ANY NUMBER of x2many hops.

    Superset of ``ks_walk_path`` (which stays many2one-only for snapshot
    fields and the aggregate correlation key), used for report columns.

    Multiple lists in one path (e.g. ``milestone_ids.task_ids.name``) are
    supported: each successive list becomes another JOIN inside the one
    correlated subquery, and the collapse aggregates over the fully expanded
    chain at the end, so the fan-out never escapes the parentheses.

    Returns a dict:
      ``prefix``       many2one path from the base model to the model that
                       OWNS the FIRST list ('' when it is on the base model)
      ``owner_model``  technical name of that owning model
      ``x2many``       the FIRST x2many ``Field``, or None for a plain m2o
                       path (kept for callers that only care whether the path
                       crosses a list at all)
      ``hops``         one entry per list crossed, in order, each
                       ``{'field': <x2many Field>, 'owner_model': <model that
                       defines it>, 'sub_path': <many2one path to walk after
                       landing on its comodel - leading to the next list's
                       owner, or, for the last hop, down to the final
                       scalar>}``. Empty for a plain many2one path.
      ``child_model``  comodel of the LAST list - the model the leaf value and
                       the user's List Filter are read against - or None
      ``suffix``       ``sub_path`` of the LAST hop ('' when the list itself
                       is the final segment - only a Count collapse is
                       meaningful then)
      ``field``        the terminal scalar ``Field``, or None when a list is
                       the final segment
    """
    if not path:
        raise ValidationError(_("Pick the source field."))
    segments = path.split('.')
    hops = []
    prefix = ''
    first_owner_model = model_name
    current = model_name
    # many2one segments seen since the last list (or since the base model)
    pending = []
    final_field = None

    for index, segment in enumerate(segments):
        model = env.get(current)
        if model is None:
            raise ValidationError(_("Model %s does not exist.", current))
        field = model._fields.get(segment)
        if field is None:
            raise ValidationError(_(
                "Field %(f)s does not exist on %(m)s.", f=segment, m=current))
        if getattr(field, 'company_dependent', False) and not allow_company_dependent:
            raise ValidationError(KS_COMPANY_DEPENDENT_ERROR(segment))
        is_last = index == len(segments) - 1

        if field.type in KS_X2MANY_TYPES:
            # Close off whatever many2one hops preceded this list: before the
            # FIRST list they are the outer-query prefix, afterwards they
            # belong to the previous hop as the way to reach THIS list's owner.
            if hops:
                hops[-1]['sub_path'] = '.'.join(pending)
            else:
                prefix = '.'.join(pending)
                first_owner_model = current
            hops.append({
                'field': field,
                'owner_model': current,
                'sub_path': '',
            })
            pending = []
            current = field.comodel_name
            continue

        if is_last:
            final_field = field
            pending.append(segment)
            break

        if field.type not in KS_TRAVERSABLE_TYPES:
            raise ValidationError(_(
                "%(f)s is a %(t)s and cannot be drilled into.",
                f=segment, t=field.type))
        pending.append(segment)
        current = field.comodel_name

    if hops:
        hops[-1]['sub_path'] = '.'.join(pending)

    if final_field is not None:
        if final_field.type not in KS_SCALAR_TYPES:
            raise ValidationError(_(
                "%(f)s is a %(t)s and cannot be used as a column.",
                f=segments[-1], t=final_field.type))

    return {
        'prefix': prefix,
        'owner_model': first_owner_model,
        'x2many': hops[0]['field'] if hops else None,
        'hops': hops,
        'child_model': hops[-1]['field'].comodel_name if hops else None,
        'suffix': hops[-1]['sub_path'] if hops else '',
        'field': final_field,
    }


def ks_x2many_sql_info(env, field, owner_model_name):
    """Validate that ``field`` (a one2many/many2many) is backed by real tables
    and return everything needed to correlate a subquery back to its owner.

    A One2many's ``inverse_name`` may be absent, non-stored/computed, or a
    ``many2one_reference`` generic FK; a Many2many's ``relation``/``column1``/
    ``column2`` are None when the field is not stored - so all are checked.

    Applying ``get_comodel_domain()`` is not optional: for a One2many it adds
    the res_model-style discriminator, without which a list over a generically
    keyed model (mail.message, ir.attachment) matches other models' rows that
    share the same numeric id.
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
    """A dotted field path of arbitrary depth, set by the
    ``ks_field_chain_picker`` widget. Concrete models supply ``base_model_id``.
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
