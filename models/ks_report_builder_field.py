import ast

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools.sql import make_identifier

from .ks_field_path_mixin import (
    KS_TTYPE_OVERRIDE, ks_slugify, ks_split_path, ks_walk_path,
    ks_x2many_sql_info,
)

# A correlated subquery collapses a child table to one scalar per base row,
# keeping the outer query flat while reaching a table that is not walkable by
# many2one (e.g. stock.quant from a sale.order.line report). agg_function is a
# Selection, so only these five keys can ever index this dict.
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
    definition = fields.Char(
        string='Definition', compute='_compute_definition',
        help="Plain-language summary of what this column reads, so the "
             "Columns list stays readable without showing every "
             "kind-specific field at once.",
    )
    flat_model_name = fields.Char(
        string='List Model', compute='_compute_flat_model_name',
        help="Technical name of the model on the far side of the list. Used "
             "to point the list Filter's domain widget at the right model.",
    )
    flat_domain = fields.Char(
        string='List Filter',
        help="Optional filter on the rows of the list before they are "
             "reduced, e.g. only delivered lines. Written against the model "
             "the list points at. Leave empty to use every row.",
    )
    searchable = fields.Boolean(
        string='Searchable', default=True,
        help="Offer this column in the report's search bar.",
    )
    groupable = fields.Boolean(
        string='Group By', default=False,
        help="Offer this column as a Group By option in the report's search "
             "panel. Best on the dimensions you slice by (customer, product, "
             "status), not on measures.",
    )
    collapse = fields.Selection(
        selection=[
            ('list', 'List as text (unique values)'),
            ('list_all', 'List as text (one entry per record)'),
            ('first', 'First'),
            ('last', 'Last'),
            ('count', 'Count'),
        ],
        string='When Multiple',
        help="Only used when the Field path goes through a list (one2many or "
             "many2many). Such a path matches many rows per record, so they "
             "must be reduced to one value: join them into one text, take the "
             "first/last one, or count them.\n\n"
             "The two list modes differ ONLY in de-duplication, and the "
             "difference matters whenever two different records can share the "
             "same displayed text:\n"
             "- 'unique values' de-duplicates on the TEXT, so three tasks all "
             "named 'Design' read as one entry. Tidy, but the number of "
             "entries then no longer matches a Count column over the same "
             "path, and records are silently invisible.\n"
             "- 'one entry per record' keeps every matching record, so the "
             "entries always reconcile with a Count over the same path "
             "('Design, Design, Design' = 3). Pick this when the list has to "
             "be auditable against a count.",
    )

    # Aggregate lookup: a scalar SUM/COUNT/AVG/MAX/MIN over a child table,
    # correlated on a shared many2one dimension - never a join.
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
    agg_model_technical_name = fields.Char(
        related='agg_model_id.model',
        string='From Model Technical Name',
        help="Used to point the Filter domain widget at the From Model - it "
             "needs the technical name as a string, not the agg_model_id "
             "many2one value.",
    )
    agg_domain = fields.Char(
        string='Filter',
        help="Restricts which rows of the From Model are aggregated, e.g. "
             "only internal locations for a stock quantity lookup. Written "
             "against the From Model's own fields. Leave empty to aggregate "
             "every matching row.",
    )
    agg_link_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Link Field',
        ondelete='cascade',
        # company_dependent=False: excluded from the picker's domain rather
        # than rejected afterwards. See KS_COMPANY_DEPENDENT_ERROR.
        domain="[('model_id', '=', agg_model_id), ('ttype', '=', 'many2one'),"
               " ('store', '=', True), ('company_dependent', '=', False)]",
        help="The many2one field on the From Model that points to the SAME "
             "model as Correlate On (e.g. Product on Stock Quant).",
    )
    agg_measure_field_id = fields.Many2one(
        comodel_name='ir.model.fields',
        string='Aggregate Field',
        ondelete='cascade',
        domain="[('model_id', '=', agg_model_id),"
               " ('ttype', 'in', ['integer', 'float', 'monetary']),"
               " ('store', '=', True), ('company_dependent', '=', False)]",
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

    def _ks_walk_path(self):
        """Allow x2many hops, which the query builder collapses with a
        correlated subquery, so the mixin's ``_check_path`` accepts them.

        Returns the terminal scalar Field, or None when the path ends on the
        list itself (Count).
        """
        self.ensure_one()
        if not self.base_model_id:
            raise ValidationError(_("Pick the source model first."))
        info = ks_split_path(
            self.env, self.base_model_id.model, self.path,
            allow_company_dependent=bool(self.report_id.property_company_id))
        if not info['hops']:
            if self.collapse:
                raise ValidationError(_(
                    "Column %s: 'When Multiple' only applies to a path that "
                    "goes through a list - this one doesn't, so leave it "
                    "empty.", self.label or '?'))
            if self.flat_domain and self.flat_domain != '[]':
                raise ValidationError(_(
                    "Column %s: 'List Filter' only applies to a path that "
                    "goes through a list - this one doesn't, so leave it "
                    "empty.", self.label or '?'))
            return info['field']
        # Fail at save time, not at Deploy, if any list in the chain lacks
        # real tables or a usable inverse column. Checked per hop.
        for hop in info['hops']:
            ks_x2many_sql_info(self.env, hop['field'], hop['owner_model'])
        last_x2many = info['hops'][-1]['field']
        if not self.collapse:
            raise ValidationError(_(
                "Column %(c)s: %(f)s is a list, so one record matches many "
                "rows. Choose 'When Multiple' to say how they should be "
                "reduced to a single value.",
                c=self.label or '?', f=info['x2many'].name))
        if info['field'] is None and self.collapse != 'count':
            raise ValidationError(_(
                "Column %(c)s: the path stops on the list %(f)s itself, so "
                "the only thing that can be shown is how many rows it has - "
                "set 'When Multiple' to Count, or drill into a field of "
                "%(m)s.",
                c=self.label or '?', f=last_x2many.name,
                m=last_x2many.comodel_name))
        if self.collapse in ('list', 'list_all') and info['field'] is not None \
                and info['field'].type == 'many2one':
            # The builder follows the relation one more hop to _rec_name so
            # the text reads "Chair, Desk" rather than raw ids; reject only
            # when the comodel has no stored name to read.
            comodel = self.env.get(info['field'].comodel_name)
            # NB: `comodel and comodel._rec_name` would be wrong - an empty
            # recordset is falsy, so the `and` yields the recordset.
            rec_name = comodel._rec_name if comodel is not None else None
            rec_field = comodel._fields.get(rec_name) if rec_name else None
            if rec_field is None or not (
                    rec_field.store or getattr(rec_field, 'inherited', False)):
                raise ValidationError(_(
                    "Column %(c)s: %(m)s has no stored name column, so its "
                    "records can't be listed as text. Drill one level further "
                    "into a specific field of %(m)s, or use First/Last.",
                    c=self.label or '?', m=info['field'].comodel_name))
        if self.flat_domain and self.flat_domain != '[]':
            try:
                parsed = ast.literal_eval(self.flat_domain)
                assert isinstance(parsed, list)
            except (ValueError, SyntaxError, AssertionError) as error:
                raise ValidationError(_(
                    "Column %s: List Filter must be a valid domain list.",
                    self.label or '?')) from error
            from odoo.orm.domains import Domain
            try:
                # Written against the LAST list's model, where the filtered
                # rows and the leaf value both live.
                Domain(parsed).optimize_full(
                    self.env[info['child_model']])
            except Exception as error:
                raise ValidationError(_(
                    "Column %(c)s: invalid List Filter (%(e)s).",
                    c=self.label or '?', e=str(error))) from error
        return info['field']

    @api.depends('kind', 'collapse', 'agg_measure_field_id', 'agg_function')
    def _compute_ttype_relation(self):
        super()._compute_ttype_relation()
        for line in self:
            if line.kind == 'path':
                line._ks_compute_flatten_ttype()
                continue
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

    @api.depends('kind', 'path', 'base_model_id')
    def _compute_flat_model_name(self):
        for line in self:
            line.flat_model_name = False
            if line.kind != 'path' or not line.path or not line.base_model_id:
                continue
            try:
                info = ks_split_path(
                    line.env, line.base_model_id.model, line.path,
                    allow_company_dependent=bool(line.report_id.property_company_id))
            except ValidationError:
                continue
            # The LAST list's model: the List Filter and the leaf both apply
            # to the far end of the chain.
            line.flat_model_name = info['child_model']

    @api.depends('kind', 'path', 'collapse', 'expression', 'agg_function',
                 'agg_model_id', 'agg_measure_field_id', 'agg_base_path',
                 'flat_domain')
    def _compute_definition(self):
        labels = dict(self._fields['collapse'].selection)
        for line in self:
            if line.kind == 'expression':
                line.definition = line.expression or ''
            elif line.kind == 'aggregate':
                parts = '%s(%s.%s)' % (
                    (line.agg_function or '?').upper(),
                    line.agg_model_id.model or '?',
                    line.agg_measure_field_id.name or '?')
                # Empty agg_base_path means "correlate on the base record
                # itself", not an incomplete column. Wording matches
                # ks_load_report in ks_report_designer.py - keep in sync.
                line.definition = '%s per %s' % (
                    parts, line.agg_base_path or _('this record'))
            elif line.collapse:
                line.definition = '%s of %s%s' % (
                    labels.get(line.collapse, line.collapse), line.path or '?',
                    ' (filtered)' if line.flat_domain and line.flat_domain != '[]' else '')
            else:
                line.definition = line.path or ''

    def _ks_compute_flatten_ttype(self):
        """Fix up ttype/relation for a path that crosses a list.

        The mixin types the column from the terminal field, which is right for
        First/Last (one real row's value, type preserved). List-as-text and
        Count return something else.
        """
        self.ensure_one()
        if not self.path or not self.base_model_id:
            return
        try:
            info = ks_split_path(
                self.env, self.base_model_id.model, self.path,
                allow_company_dependent=bool(self.report_id.property_company_id))
        except ValidationError:
            return  # _check_path reports the real error at save time
        if not info['hops']:
            return
        if self.collapse == 'count':
            self.ttype, self.relation = 'integer', False
        elif self.collapse in ('list', 'list_all'):
            self.ttype, self.relation = 'char', False

    @api.depends('label', 'path', 'kind', 'collapse', 'flat_domain')
    def _compute_column_name(self):
        for line in self:
            source = line.path if line.kind == 'path' else line.label
            name = ks_slugify(source) or 'column'
            if line.kind == 'path' and line.collapse:
                # A First/Last pair shares one path and differs only by
                # collapse, so the collapse has to be part of the name.
                name = '%s_%s' % (name, line.collapse)
                if line.flat_domain and line.flat_domain != '[]':
                    # Two filtered lists over one path+collapse differ only
                    # by a filter too long for a column name, so fold in the
                    # label instead.
                    name = '%s_%s' % (name, ks_slugify(line.label) or 'filtered')
            # PostgreSQL and ir.model.fields both cap identifiers at 63
            # chars, which a multi-hop path plus collapse and label overflows.
            # make_identifier truncates to 54 and appends a crc32 -
            # deterministic, so a redeploy keeps the same name, and a no-op
            # for anything already within the limit.
            line.column_name = make_identifier('x_%s' % name)

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
                        # Not required: empty means "correlate on the base
                        # record itself", which is what a one-row-per-X report
                        # needs. No model has a many2one to itself.
                        'agg_model_id', 'agg_link_field_id',
                        'agg_measure_field_id', 'agg_function')
                    if not line[field_name]
                ]
                if missing:
                    raise ValidationError(_(
                        "Column %s: complete the aggregate lookup - source "
                        "model, link field, aggregated field and aggregator "
                        "are all required. (Correlate On is optional: leave "
                        "it empty to total against each record of this "
                        "report's own base model.)", line.label or '?'))

    @api.constrains('kind', 'path', 'collapse', 'flat_domain')
    def _check_flatten(self):
        """The mixin's own _check_path only watches path/base_model_id, so a
        change to collapse or the list filter alone would slip past it."""
        for line in self:
            if line.kind == 'path' and line.path and line.base_model_id:
                line._ks_walk_path()

    @api.constrains('kind', 'agg_domain')
    def _check_agg_domain(self):
        for line in self:
            if line.kind != 'aggregate' or not line.agg_domain:
                continue
            try:
                parsed = ast.literal_eval(line.agg_domain)
                assert isinstance(parsed, list)
            except (ValueError, SyntaxError, AssertionError) as error:
                raise ValidationError(_(
                    "Column %s: Filter must be a valid domain list.",
                    line.label)) from error
            if not line.agg_model_id:
                continue  # _check_definition already raises for incompleteness
            from odoo.orm.domains import Domain
            try:
                Domain(parsed).optimize_full(line.env[line.agg_model_id.model])
            except Exception as error:
                raise ValidationError(_(
                    "Column %(c)s: invalid Filter (%(e)s).",
                    c=line.label, e=str(error))) from error

    @api.constrains('kind', 'agg_base_path', 'agg_model_id', 'agg_link_field_id',
                     'agg_measure_field_id')
    def _check_aggregate(self):
        for line in self:
            if line.kind != 'aggregate':
                continue
            if not (line.agg_model_id and line.agg_link_field_id
                    and line.agg_measure_field_id):
                continue  # _check_definition already raises for incompleteness
            if line.agg_base_path:
                base_field = ks_walk_path(
                    line.env, line.base_model_id.model, line.agg_base_path,
                    allow_company_dependent=bool(line.report_id.property_company_id))
                if base_field.type != 'many2one':
                    raise ValidationError(_(
                        "Column %s: Correlate On must be a many2one field.",
                        line.label))
                dimension = base_field.comodel_name
            else:
                # Empty Correlate On = correlate on the base record itself
                # (WHERE t.link = base.id), which is what makes a
                # one-row-per-X report possible.
                dimension = line.base_model_id.model
            if line.agg_link_field_id.model_id != line.agg_model_id:
                raise ValidationError(_(
                    "Column %s: the link field must belong to the source "
                    "model.", line.label))
            if line.agg_link_field_id.relation != dimension:
                raise ValidationError(_(
                    "Column %(c)s: the link field (%(l)s, related to "
                    "%(lr)s) doesn't match what it is correlated on "
                    "(%(br)s) - both must point to the same model.",
                    c=line.label, l=line.agg_link_field_id.name,
                    lr=line.agg_link_field_id.relation, br=dimension))
            if line.agg_measure_field_id.model_id != line.agg_model_id:
                raise ValidationError(_(
                    "Column %s: the aggregated field must belong to the "
                    "source model.", line.label))

    def _ks_duplicate_signature(self):
        """Everything that decides what a column computes, Label excluded.

        Two columns with the same signature return identical data for every
        row.
        """
        self.ensure_one()

        def _domain(text):
            # '' , False and '[]' all mean "no filter", and two spellings of
            # the same domain must not read as two different filters.
            if not text:
                return '[]'
            try:
                return repr(ast.literal_eval(text))
            except (ValueError, SyntaxError):
                return text.strip()

        if self.kind == 'path':
            return ('path', self.path, self.collapse or False,
                    _domain(self.flat_domain))
        if self.kind == 'expression':
            return ('expression', (self.expression or '').strip())
        return ('aggregate', self.agg_base_path or '',
                self.agg_model_id.id, self.agg_link_field_id.id,
                self.agg_measure_field_id.id, self.agg_function,
                _domain(self.agg_domain))

    @api.constrains('report_id', 'kind', 'path', 'collapse', 'flat_domain',
                    'expression', 'agg_base_path', 'agg_model_id',
                    'agg_link_field_id', 'agg_measure_field_id',
                    'agg_function', 'agg_domain')
    def _check_duplicate_column(self):
        """Refuse two columns that compute exactly the same thing.

        _check_unique_column below only catches these incidentally, via the
        derived technical name: a filtered flatten folds the label into its
        name, and expression/aggregate columns name themselves from the label
        alone, so identical ones can coexist. Comparing what a column computes
        closes all three cases.
        """
        for line in self:
            if not line.report_id:
                continue
            signature = line._ks_duplicate_signature()
            for other in line.report_id.field_ids - line:
                if other._ks_duplicate_signature() == signature:
                    raise ValidationError(_(
                        "Columns %(a)s and %(b)s are the same column - they "
                        "have identical settings and would show identical "
                        "values. Remove one, or change what makes them "
                        "different (for a list, that is usually the "
                        "'When Multiple' mode or the Filter).",
                        a=other.label or other.column_name,
                        b=line.label or line.column_name))

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
