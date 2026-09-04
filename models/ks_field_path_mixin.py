import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

# Column types that can be selected.
KS_SCALAR_TYPES = (
    'char', 'text', 'selection', 'integer', 'float', 'monetary',
    'boolean', 'date', 'datetime', 'many2one',
)
# Only many2one may be traversed: crossing a one2many/many2many fans out rows
# and every measure would silently double count.
KS_TRAVERSABLE_TYPES = ('many2one',)

# monetary needs a currency_field companion and selection needs
# ir.model.fields.selection rows, neither of which a generated column can
# supply reliably, so both degrade to a simpler type.
KS_TTYPE_OVERRIDE = {'monetary': 'float', 'selection': 'char'}

KS_SCALAR_DOMAIN = (
    "[('model_id', '=', %(model)s), ('ttype', 'in', %(types)s),"
    " '|', ('store', '=', True), ('related', '!=', False)]"
)


def ks_slugify(value):
    return re.sub(r'[^a-z0-9]+', '_', (value or '').lower()).strip('_')


class KsFieldPathMixin(models.AbstractModel):
    """Three chained dropdowns that resolve to a dotted field path.

    Nothing is typed: each level lists the stored fields of the model reached by
    the previous level. Concrete models supply ``base_model_id``.
    """
    _name = 'ks.field.path.mixin'
    _description = 'Field Path Picker'

    base_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Source Model',
        ondelete='cascade',
    )
    field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Field',
        ondelete='cascade',
        domain=KS_SCALAR_DOMAIN % {'model': 'base_model_id', 'types': list(KS_SCALAR_TYPES)},
    )
    level2_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Level 2 Model',
        compute='_compute_level_models',
    )
    sub_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Then',
        ondelete='cascade',
        domain=KS_SCALAR_DOMAIN % {'model': 'level2_model_id', 'types': list(KS_SCALAR_TYPES)},
    )
    level3_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Level 3 Model',
        compute='_compute_level_models',
    )
    sub_sub_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Then Again',
        ondelete='cascade',
        domain=KS_SCALAR_DOMAIN % {'model': 'level3_model_id', 'types': list(KS_SCALAR_TYPES)},
    )
    path = fields.Char(string='Field Path', compute='_compute_path', store=True)
    ttype = fields.Char(string='Type', compute='_compute_path', store=True)
    relation = fields.Char(string='Relation', compute='_compute_path', store=True)

    def _ks_path_active(self):
        """Concrete models may disable path resolution for some records."""
        self.ensure_one()
        return True

    @api.depends('field_id', 'sub_field_id')
    def _compute_level_models(self):
        ir_model = self.env['ir.model']
        for line in self:
            level2 = level3 = ir_model
            if line.field_id.ttype in KS_TRAVERSABLE_TYPES and line.field_id.relation:
                level2 = ir_model.sudo()._get(line.field_id.relation)
            if line.sub_field_id.ttype in KS_TRAVERSABLE_TYPES and line.sub_field_id.relation:
                level3 = ir_model.sudo()._get(line.sub_field_id.relation)
            line.level2_model_id = level2
            line.level3_model_id = level3

    @api.depends('field_id', 'sub_field_id', 'sub_sub_field_id')
    def _compute_path(self):
        for line in self:
            if not line._ks_path_active():
                line.path = False
                line.ttype = 'float'
                line.relation = False
                continue
            segments = []
            final = self.env['ir.model.fields']
            for candidate in (line.field_id, line.sub_field_id, line.sub_sub_field_id):
                if not candidate:
                    break
                segments.append(candidate.name)
                final = candidate
            line.path = '.'.join(segments) or False
            raw_type = final.ttype or False
            line.ttype = KS_TTYPE_OVERRIDE.get(raw_type, raw_type)
            line.relation = final.relation if raw_type == 'many2one' else False

    @api.constrains('field_id', 'sub_field_id', 'sub_sub_field_id')
    def _check_traversal(self):
        for line in self:
            for parent, child in ((line.field_id, line.sub_field_id),
                                  (line.sub_field_id, line.sub_sub_field_id)):
                if child and parent.ttype not in KS_TRAVERSABLE_TYPES:
                    raise ValidationError(_(
                        "%(f)s is a %(t)s and cannot be drilled into. Only "
                        "many2one relations can be followed, otherwise totals "
                        "would be counted more than once.",
                        f=parent.name, t=parent.ttype))

    @api.onchange('field_id', 'sub_field_id', 'sub_sub_field_id')
    def _onchange_field_path(self):
        if not self.field_id or self.field_id.ttype not in KS_TRAVERSABLE_TYPES:
            self.sub_field_id = False
        if not self.sub_field_id or self.sub_field_id.ttype not in KS_TRAVERSABLE_TYPES:
            self.sub_sub_field_id = False
