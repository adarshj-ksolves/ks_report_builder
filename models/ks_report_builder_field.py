from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .ks_field_path_mixin import ks_slugify


class KsReportBuilderField(models.Model):
    _name = 'ks.report.builder.field'
    _description = 'Report Builder Column'
    _inherit = ['ks.field.path.mixin']
    _order = 'sequence, id'

    report_id = fields.Many2one(
        comodel_name='ks.report.builder',
        string='Report',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)
    kind = fields.Selection(
        selection=[('path', 'Field'), ('expression', 'Computed')],
        string='Column Type',
        default='path',
        required=True,
    )
    label = fields.Char(string='Label', required=True)
    column_name = fields.Char(
        string='Technical Name', compute='_compute_column_name', store=True)
    expression = fields.Char(
        string='Expression',
        help="Arithmetic over other numeric column labels of this report, e.g. "
             "gross_amount - net_amount. Only + - * / and numbers are allowed.",
    )
    base_model_id = fields.Many2one(
        related='report_id.model_id',
        string='Source Model',
        readonly=True,
    )

    def _ks_path_active(self):
        self.ensure_one()
        return self.kind == 'path'

    @api.depends('kind')
    def _compute_path(self):
        return super()._compute_path()

    @api.depends('label', 'path', 'kind')
    def _compute_column_name(self):
        for line in self:
            source = line.path if line.kind == 'path' else line.label
            line.column_name = 'x_%s' % (ks_slugify(source) or 'column')

    @api.constrains('kind', 'field_id', 'expression')
    def _check_definition(self):
        for line in self:
            if line.kind == 'path' and not line.field_id:
                raise ValidationError(
                    _("Column %s: pick a field.", line.label or '?'))
            if line.kind == 'expression' and not line.expression:
                raise ValidationError(
                    _("Column %s: enter an expression.", line.label or '?'))

    @api.constrains('report_id', 'column_name')
    def _check_unique_column(self):
        for line in self:
            if self.search_count([
                ('report_id', '=', line.report_id.id),
                ('column_name', '=', line.column_name),
                ('id', '!=', line.id),
            ]):
                raise ValidationError(_(
                    "Two columns resolve to the same technical name %s. "
                    "Rename one of the labels.", line.column_name))

    @api.onchange('field_id', 'sub_field_id', 'sub_sub_field_id')
    def _onchange_field_path(self):
        super()._onchange_field_path()
        if not self.label:
            final = self.sub_sub_field_id or self.sub_field_id or self.field_id
            self.label = final.field_description or final.name
