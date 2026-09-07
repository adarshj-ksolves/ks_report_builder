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
    if getattr(field, 'translate', False):
        # Translated fields (translate=True) store a jsonb blob of
        # {lang_code: value} in Postgres, not a plain scalar column (Odoo
        # resolves the current language in Python at read time, not via a
        # single SQL expression) - selecting the raw column hands the
        # generated field a dict instead of a string, which the web client
        # can't render (pivot row headers show "[object Object]", grouping
        # breaks because every row's blob is a distinct dict). Hardcoding one
        # language via ->> would silently misreport for any other UI
        # language, so this is rejected rather than half-supported.
        raise ValidationError(_(
            "%(f)s is a translated field and cannot be used as a column or "
            "correlation key (its stored value is per-language, not a "
            "single value).", f=segments[-1]))
    return field


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
            raw_type = field.type
            line.ttype = KS_TTYPE_OVERRIDE.get(raw_type, raw_type)
            line.relation = field.comodel_name if raw_type == 'many2one' else False

    @api.constrains('path', 'base_model_id')
    def _check_path(self):
        for line in self:
            if line._ks_path_active() and line.path and line.base_model_id:
                line._ks_walk_path()
