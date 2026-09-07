from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .ks_field_path_mixin import KS_TTYPE_OVERRIDE, ks_slugify, ks_walk_path

# Aggregating a child table down to one scalar per base row, via a correlated
# subquery, keeps the outer query flat (still one row per base record, no
# GROUP BY - Invariant #1) while giving real cross-model access to a table
# that isn't reachable by walking many2ones forward (e.g. stock.quant from a
# sale.order.line report, related only by a shared product_id, not a
# parent-child FK). The aggregator itself is never free text: it's a
# Selection field, so only these five keys can ever reach the dict below.
KS_AGGREGATOR_SQL = {
    'sum': 'SUM',
    'count': 'COUNT',
    'avg': 'AVG',
    'max': 'MAX',
    'min': 'MIN',
}
KS_AGGREGATE_NUMERIC_TYPES = ('integer', 'float', 'monetary')


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
        selection=[
            ('path', 'Field'),
            ('expression', 'Computed'),
            ('aggregate', 'Aggregate'),
        ],
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

    # ------------------------------------------------------------------
    # Aggregate lookup: a scalar SUM/COUNT/AVG/MAX/MIN over an unrelated
    # child table, correlated back to this report's base model on a shared
    # many2one dimension - not a join, so it can never fan out rows.
    # ------------------------------------------------------------------
    agg_base_path = fields.Char(
        string='Correlate On',
        help="A many2one field on THIS report's base model - the value used "
             "to look up matching rows in the source model below (e.g. "
             "Product, if the source model also has a Product field).",
    )
    agg_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='From Model',
        ondelete='cascade',
        domain="[('transient', '=', False), ('abstract', '=', False)]",
        help="The unrelated table to aggregate from, e.g. Stock Quant.",
    )
    agg_link_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Link Field',
        ondelete='cascade',
        domain="[('model_id', '=', agg_model_id), ('ttype', '=', 'many2one'),"
               " ('store', '=', True)]",
        help="The many2one field on the From Model that points to the SAME "
             "model as Correlate On (e.g. Product on Stock Quant).",
    )
    agg_measure_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Aggregate Field',
        ondelete='cascade',
        domain="[('model_id', '=', agg_model_id),"
               " ('ttype', 'in', ['integer', 'float', 'monetary']),"
               " ('store', '=', True)]",
        help="The numeric field to aggregate, e.g. Quantity.",
    )
    agg_function = fields.Selection(
        selection=[
            ('sum', 'Sum'),
            ('count', 'Count'),
            ('avg', 'Average'),
            ('max', 'Maximum'),
            ('min', 'Minimum'),
        ],
        string='Aggregator',
    )

    def _ks_path_active(self):
        self.ensure_one()
        return self.kind == 'path'

    @api.depends('kind', 'agg_measure_field_id', 'agg_function')
    def _compute_ttype_relation(self):
        super()._compute_ttype_relation()
        for line in self:
            if line.kind != 'aggregate':
                continue
            if line.agg_function == 'count':
                line.ttype = 'integer'
            elif line.agg_measure_field_id:
                raw_type = line.agg_measure_field_id.ttype
                line.ttype = KS_TTYPE_OVERRIDE.get(raw_type, raw_type)
            else:
                line.ttype = 'float'
            line.relation = False

    @api.depends('label', 'path', 'kind')
    def _compute_column_name(self):
        for line in self:
            source = line.path if line.kind == 'path' else line.label
            line.column_name = 'x_%s' % (ks_slugify(source) or 'column')

    @api.constrains('kind', 'path', 'expression', 'agg_base_path', 'agg_model_id',
                     'agg_link_field_id', 'agg_measure_field_id', 'agg_function')
    def _check_definition(self):
        for line in self:
            if line.kind == 'path' and not line.path:
                raise ValidationError(
                    _("Column %s: pick a field.", line.label or '?'))
            if line.kind == 'expression' and not line.expression:
                raise ValidationError(
                    _("Column %s: enter an expression.", line.label or '?'))
            if line.kind == 'aggregate':
                missing = [
                    field_name for field_name in (
                        'agg_base_path', 'agg_model_id', 'agg_link_field_id',
                        'agg_measure_field_id', 'agg_function')
                    if not line[field_name]
                ]
                if missing:
                    raise ValidationError(_(
                        "Column %s: complete the aggregate lookup - correlation "
                        "field, source model, link field, aggregated field and "
                        "aggregator are all required.", line.label or '?'))

    @api.constrains('kind', 'agg_base_path', 'agg_model_id', 'agg_link_field_id',
                     'agg_measure_field_id')
    def _check_aggregate(self):
        for line in self:
            if line.kind != 'aggregate':
                continue
            if not (line.agg_base_path and line.agg_model_id
                    and line.agg_link_field_id and line.agg_measure_field_id):
                continue  # _check_definition already raises for incompleteness
            base_field = ks_walk_path(
                line.env, line.base_model_id.model, line.agg_base_path)
            if base_field.type != 'many2one':
                raise ValidationError(_(
                    "Column %s: Correlate On must be a many2one field.",
                    line.label))
            if line.agg_link_field_id.model_id != line.agg_model_id:
                raise ValidationError(_(
                    "Column %s: the link field must belong to the source "
                    "model.", line.label))
            if line.agg_link_field_id.relation != base_field.comodel_name:
                raise ValidationError(_(
                    "Column %(c)s: the link field (%(l)s, related to "
                    "%(lr)s) doesn't match Correlate On (related to "
                    "%(br)s) - both must point to the same model.",
                    c=line.label, l=line.agg_link_field_id.name,
                    lr=line.agg_link_field_id.relation, br=base_field.comodel_name))
            if line.agg_measure_field_id.model_id != line.agg_model_id:
                raise ValidationError(_(
                    "Column %s: the aggregated field must belong to the "
                    "source model.", line.label))

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
