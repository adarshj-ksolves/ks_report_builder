import ast
import logging

from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError

from .ir_model import KS_MODEL_PREFIX
from .ks_report_builder_field import KS_AGGREGATOR_SQL

_logger = logging.getLogger(__name__)

KS_NUMERIC_TYPES = ('integer', 'float')
KS_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
KS_BINOP_SYMBOLS = {ast.Add: '+', ast.Sub: '-', ast.Mult: '*'}


class KsReportBuilder(models.Model):
    _name = 'ks.report.builder'
    _description = 'Report Builder'
    _order = 'name, id'

    name = fields.Char(string='Report Name', required=True)
    model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Base Model',
        required=True,
        ondelete='cascade',
        domain="[('transient', '=', False), ('abstract', '=', False)]",
        help="One row of the report equals one record of this model.",
    )
    base_model_name = fields.Char(
        related='model_id.model', string='Base Model Technical Name',
        help="Used to point the Filter's domain widget at the base model - "
             "the widget needs the technical name as a string, not the "
             "model_id many2one value.",
    )
    field_ids = fields.One2many(
        comodel_name='ks.report.builder.field',
        inverse_name='report_id',
        string='Columns',
        copy=True,
    )
    domain = fields.Char(
        string='Filter',
        default='[]',
        help="Applied to the generated action, not baked into the query, so "
             "interactive filtering stays correct.",
    )
    group_ids = fields.Many2many(
        comodel_name='res.groups',
        string='Visible To',
        help="Groups granted read access to the generated report. Leave empty "
             "to grant it to every internal user.",
    )
    menu_parent_id = fields.Many2one(
        comodel_name='ir.ui.menu',
        string='Menu Location',
        default=lambda self: self.env.ref(
            'ks_report_builder.menu_ks_report_builder_generated',
            raise_if_not_found=False),
    )
    create_menu = fields.Boolean(string='Create Menu Entry', default=True)
    skip_company_check = fields.Boolean(
        string='Skip Multi-Company Check',
        help="Deploy without a company_id column. Only tick this on a "
             "single-company database: without it the report shows rows from "
             "every company.",
    )

    state = fields.Selection(
        selection=[('draft', 'Draft'), ('deployed', 'Deployed')],
        default='draft',
        required=True,
        readonly=True,
        copy=False,
    )
    model_name = fields.Char(string='Generated Model', readonly=True, copy=False)
    query = fields.Text(string='Compiled Query', readonly=True, copy=False)
    generated_model_id = fields.Many2one(
        comodel_name='ir.model', string='Generated Model Record',
        readonly=True, copy=False, ondelete='set null')
    action_id = fields.Many2one(
        comodel_name='ir.actions.act_window', string='Generated Action',
        readonly=True, copy=False, ondelete='set null')
    menu_id = fields.Many2one(
        comodel_name='ir.ui.menu', string='Generated Menu',
        readonly=True, copy=False, ondelete='set null')
    view_ids = fields.Many2many(
        comodel_name='ir.ui.view', string='Generated Views',
        readonly=True, copy=False)

    _model_name_uniq = models.Constraint(
        'unique(model_name)',
        "Each report must map to its own generated model.",
    )

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _ks_check_table_backed(self, model_name):
        model = self.env.get(model_name)
        if model is None:
            raise UserError(_("Model %s is not present in the registry.", model_name))
        if not model._auto:
            raise UserError(_(
                "Model %s is not backed by a real table (it is itself a query-based "
                "report), so it cannot be used or joined here.", model_name))
        return model

    def _ks_register_join(self, joins, key, parent_alias, fk_column, table):
        if key not in joins:
            joins[key] = {
                'alias': 't%d' % (len(joins) + 1),
                'parent_alias': parent_alias,
                'fk_column': fk_column,
                'table': table,
            }
        return joins[key]['alias']

    def _ks_resolve_column(self, model_name, alias, field_name, joins, prefix):
        """Return (alias, column) for a field, following _inherits delegation.

        Fields such as product.product/name physically live on product_template,
        so the delegation join is added automatically instead of failing.
        """
        model = self._ks_check_table_backed(model_name)
        field = model._fields.get(field_name)
        if field is None:
            raise UserError(_("Field %(f)s does not exist on %(m)s.",
                              f=field_name, m=model_name))
        if getattr(field, 'translate', False):
            # translate=True fields store a per-language jsonb blob, not a
            # plain scalar column - see the matching guard and rationale in
            # ks_field_path_mixin.ks_walk_path. Checked here too because
            # aggregate columns (agg_link_field_id/agg_measure_field_id)
            # reach the SQL builder without ever going through ks_walk_path.
            raise UserError(_(
                "Field %(f)s on %(m)s is a translated field and cannot be "
                "used as a column.", f=field_name, m=model_name))
        if not getattr(field, 'inherited', False):
            if not field.store:
                raise UserError(_(
                    "Field %(f)s on %(m)s is not stored and has no column to read.",
                    f=field_name, m=model_name))
            return alias, field.name
        # _inherits fields (e.g. product.product/list_price, physically on
        # product.template) default to store=False on the child model even
        # though they are backed by a real column on the parent - the check
        # above must not reject them before this delegation join is tried.
        parent_field_name, remainder = field.related.split('.', 1)
        parent_field = model._fields[parent_field_name]
        parent_model = self._ks_check_table_backed(parent_field.comodel_name)
        key = '%s@%s' % (prefix, parent_field_name)
        parent_alias = self._ks_register_join(
            joins, key, alias, parent_field.name, parent_model._table)
        return self._ks_resolve_column(
            parent_field.comodel_name, parent_alias, remainder, joins, key + '.')

    def _ks_resolve_path(self, path, joins):
        """Validate a dotted path and register every join it needs."""
        self.ensure_one()
        model_name = self.model_id.model
        alias = 'base'
        prefix = ''
        segments = (path or '').split('.')
        if not path:
            raise UserError(_("Empty field path."))
        for index, segment in enumerate(segments):
            model = self._ks_check_table_backed(model_name)
            field = model._fields.get(segment)
            if field is None:
                raise UserError(_("Field %(f)s does not exist on %(m)s.",
                                  f=segment, m=model_name))
            resolved_alias, column = self._ks_resolve_column(
                model_name, alias, segment, joins, prefix)
            if index == len(segments) - 1:
                return resolved_alias, column
            if field.type != 'many2one':
                raise UserError(_(
                    "Cannot drill through %(f)s (%(t)s). Only many2one relations "
                    "can be followed.", f=segment, t=field.type))
            target = self._ks_check_table_backed(field.comodel_name)
            key = prefix + segment
            alias = self._ks_register_join(
                joins, key, resolved_alias, column, target._table)
            model_name = field.comodel_name
            prefix = key + '.'
        raise UserError(_("Could not resolve path %s.", path))

    # ------------------------------------------------------------------
    # Aggregate lookup (scalar correlated subquery over an unrelated table)
    # ------------------------------------------------------------------

    def _ks_build_aggregate_subquery(self, line, joins):
        """Return SQL for a scalar SUM/COUNT/AVG/MAX/MIN over ``agg_model_id``,
        correlated back to this row on a shared many2one dimension.

        This is a correlated subquery, not a join: it collapses the child
        table down to one number entirely inside its own parentheses, so the
        outer query stays exactly one row per base record - no GROUP BY
        anywhere, same as every other column (Invariant #1). The correlation
        key on the base side is resolved through the normal path machinery
        (_ks_resolve_path), so multi-hop correlation (e.g. move_id.partner_id)
        reuses the same joins as any other column.

        ``agg_domain``, if set, is written against agg_model_id's own field
        names and restricts which rows of the child table are aggregated -
        e.g. "only internal locations" for a stock.quant lookup (see the
        known limitation in CLAUDE.md §8, now closed by this). A domain leaf
        that drills through a relation (e.g. order_id.state) needs its own
        JOIN inside the subquery - Odoo's domain compiler registers that join
        on the ``Query`` object it's given (an aliased "t__order_id" table),
        so the whole subquery MUST be built through that same ``Query``
        (its ``subselect()``), not hand-written as a bare "FROM table t" -
        otherwise the compiled WHERE references an alias with no
        corresponding FROM-clause entry and Postgres rejects the query.
        """
        self.ensure_one()
        from odoo.orm.domains import Domain
        from odoo.tools.query import Query
        from odoo.tools.sql import SQL

        base_alias, base_column = self._ks_resolve_path(line.agg_base_path, joins)
        target_model = self._ks_check_table_backed(line.agg_model_id.model)
        sql_func = KS_AGGREGATOR_SQL[line.agg_function]

        query = Query(self.env, 't', SQL.identifier(target_model._table))
        query.add_where(SQL(
            "%s = %s",
            SQL.identifier('t', line.agg_link_field_id.name),
            SQL.identifier(base_alias, base_column),
        ))
        parsed_domain = ast.literal_eval(line.agg_domain or '[]')
        if parsed_domain:
            compiled = Domain(parsed_domain).optimize_full(target_model)
            query.add_where(compiled._to_sql(target_model, 't', query))

        measure_sql = SQL(
            "%s(%s)", SQL(sql_func), SQL.identifier('t', line.agg_measure_field_id.name))
        subselect = query.subselect(measure_sql)
        return self.env.cr.mogrify(subselect.code, subselect.params).decode()

    # ------------------------------------------------------------------
    # Expression compiler
    # ------------------------------------------------------------------

    def _ks_compile_expression(self, expression, numeric_columns):
        """Compile arithmetic over declared numeric columns into SQL.

        An AST whitelist, not a blacklist: only declared column names, numeric
        literals and + - * / can survive, so nothing reaches the database as
        anything other than arithmetic.
        """
        aliases = {}
        for column in numeric_columns:
            aliases[column] = column
            if column.startswith('x_'):
                aliases[column[2:]] = column

        try:
            tree = ast.parse(expression, mode='eval').body
        except SyntaxError as error:
            raise UserError(_("Invalid expression: %s", expression)) from error

        def render(node):
            if isinstance(node, ast.Name):
                if node.id not in aliases:
                    raise UserError(_(
                        "Unknown column %(c)s in expression. Available numeric "
                        "columns: %(a)s",
                        c=node.id, a=', '.join(sorted(aliases)) or _("none")))
                return '"%s"' % aliases[node.id]
            if isinstance(node, ast.Constant):
                if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                    raise UserError(_("Only numeric literals are allowed."))
                return str(node.value)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return '-(%s)' % render(node.operand)
            if isinstance(node, ast.BinOp) and isinstance(node.op, KS_ALLOWED_BINOPS):
                left = render(node.left)
                right = render(node.right)
                if isinstance(node.op, ast.Div):
                    return '(%s / NULLIF(%s, 0))' % (left, right)
                return '(%s %s %s)' % (left, KS_BINOP_SYMBOLS[type(node.op)], right)
            raise UserError(_(
                "Unsupported construct in expression %s. Only + - * / , numbers "
                "and column names are allowed.", expression))

        return render(tree)

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    def _ks_build_query(self):
        """Return a flat SELECT: one row per base record, no GROUP BY.

        Aggregation is left to Odoo's pivot/list group-by so that filters are
        applied before aggregation and every grouping stays available.

        Expression columns are computed in an outer SELECT over a subquery of
        the path columns, not inline in the same SELECT list: a column alias
        defined in a SELECT list is not visible to a sibling expression in
        that same list (standard SQL scoping, enforced by PostgreSQL), so
        referencing e.g. "x_quantity" from another expression in the same
        SELECT would fail with "column does not exist". Wrapping in a
        subquery turns "x_quantity" into a real output column of the derived
        table, which CAN be referenced from the outer SELECT.
        """
        self.ensure_one()
        joins = {}
        path_selections = []
        numeric_columns = []

        # Aggregate columns are resolved alongside path columns, not in the
        # outer expression layer below: a correlated subquery references
        # base/join aliases directly, the same as a plain path column, and
        # never needs to reference a sibling column's own alias the way an
        # arithmetic expression does.
        inner_lines = self.field_ids.filtered(lambda line: line.kind in ('path', 'aggregate'))
        for line in inner_lines:
            if line.kind == 'path':
                alias, column = self._ks_resolve_path(line.path, joins)
                path_selections.append('%s."%s" AS "%s"' % (alias, column, line.column_name))
            else:
                selection_sql = self._ks_build_aggregate_subquery(line, joins)
                path_selections.append('%s AS "%s"' % (selection_sql, line.column_name))
            if line.ttype in KS_NUMERIC_TYPES:
                numeric_columns.append(line.column_name)

        base_table = self._ks_check_table_backed(self.model_id.model)._table
        join_sql = ''.join(
            ' LEFT JOIN "%(table)s" %(alias)s'
            ' ON %(alias)s.id = %(parent)s."%(fk)s"' % {
                'table': join['table'],
                'alias': join['alias'],
                'parent': join['parent_alias'],
                'fk': join['fk_column'],
            }
            for join in joins.values()
        )
        inner_query = 'SELECT base.id AS id, %s FROM "%s" base%s' % (
            ', '.join(path_selections), base_table, join_sql)

        expression_lines = self.field_ids.filtered(lambda line: line.kind == 'expression')
        if not expression_lines:
            return inner_query

        outer_selections = ['ks_base.id AS id'] + [
            'ks_base."%s" AS "%s"' % (line.column_name, line.column_name)
            for line in inner_lines
        ]
        for line in expression_lines:
            compiled = self._ks_compile_expression(line.expression, numeric_columns)
            outer_selections.append('(%s)::numeric AS "%s"' % (compiled, line.column_name))
        return 'SELECT %s FROM (%s) ks_base' % (', '.join(outer_selections), inner_query)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @api.constrains('domain')
    def _check_domain(self):
        for report in self:
            try:
                parsed = ast.literal_eval(report.domain or '[]')
                assert isinstance(parsed, list)
            except (ValueError, SyntaxError, AssertionError) as error:
                raise ValidationError(
                    _("Filter must be a valid domain list.")) from error

    def _ks_translate_domain(self):
        """Rewrite the Filter from base-model field paths to generated columns.

        The Filter is written and edited against the BASE model (the domain
        widget needs a real model to offer field choices), but it ends up on
        the GENERATED model's action - that model only has the columns this
        report explicitly declared (x_-prefixed), not the base model's own
        field names. A leaf can only be translated if its field path exactly
        matches one of this report's own "path" columns.
        """
        self.ensure_one()
        parsed = ast.literal_eval(self.domain or '[]')
        path_to_column = {
            line.path: line.column_name
            for line in self.field_ids
            if line.kind == 'path'
        }
        translated = []
        for item in parsed:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                field_expr, operator, value = item
                if field_expr == 'id':
                    translated.append((field_expr, operator, value))
                    continue
                column_name = path_to_column.get(field_expr)
                if column_name is None:
                    raise UserError(_(
                        "The filter references %(f)s, which is not one of "
                        "this report's columns. Add a column for it (any "
                        "label) before deploying, or remove it from the "
                        "filter.", f=field_expr))
                translated.append((column_name, operator, value))
            else:
                translated.append(item)
        return translated

    def _ks_validate(self):
        self.ensure_one()
        if not self.field_ids:
            raise UserError(_("Add at least one column before deploying."))
        self._ks_translate_domain()
        base_model = self._ks_check_table_backed(self.model_id.model)
        if 'company_id' in base_model._fields and not self.skip_company_check:
            has_company = any(
                line.kind == 'path' and line.path == 'company_id'
                for line in self.field_ids)
            if not has_company:
                raise UserError(_(
                    "%s is a multi-company model. Add a column for its "
                    "Company field so record rules can filter the report, or "
                    "tick Skip Multi-Company Check if this database has a "
                    "single company.", self.model_id.name))

    # ------------------------------------------------------------------
    # Deploy / undeploy
    # ------------------------------------------------------------------

    def _ks_model_name(self):
        self.ensure_one()
        return '%s%d' % (KS_MODEL_PREFIX, self.id)

    def _ks_field_vals(self):
        self.ensure_one()
        vals_list = []
        for line in self.field_ids:
            vals = {
                'name': line.column_name,
                'field_description': line.label,
                'ttype': line.ttype or 'char',
                'state': 'manual',
                'store': True,
                'readonly': True,
                'copied': False,
            }
            if line.ttype == 'many2one':
                vals.update({'relation': line.relation, 'on_delete': 'set null'})
            vals_list.append(vals)
        return vals_list

    def action_deploy(self):
        for report in self:
            report._ks_validate()
            report.action_undeploy()
            query = report._ks_build_query()
            model_name = report._ks_model_name()
            report.write({'model_name': model_name, 'query': query})
            # The query is read back with raw SQL during registry setup, which
            # happens inside ir.model.create below, so it must be in the
            # database already.
            report.env.flush_all()
            report._ks_smoke_test(query)
            generated_model = report.env['ir.model'].sudo().create({
                'name': report.name,
                'model': model_name,
                'state': 'manual',
                'field_id': [Command.create(vals) for vals in report._ks_field_vals()],
            })
            report.generated_model_id = generated_model
            report._ks_create_access()
            report._ks_create_ui()
            report.state = 'deployed'
        return {"type":"ir.actions.client", "tag":"reload"}

    def _ks_smoke_test(self, query):
        """Run the query once, returning no rows, so a broken report fails here
        rather than after the model exists."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT * FROM (%s) ks_probe WHERE FALSE' % query)
        except Exception as error:
            raise UserError(_(
                "The generated query was rejected by the database:\n\n%s", error
            )) from error

    def _ks_create_access(self):
        self.ensure_one()
        access_model = self.env['ir.model.access'].sudo()
        groups = self.group_ids or self.env.ref('base.group_user')
        for group in groups:
            access_model.create({
                'name': '%s read access' % self.model_name,
                'model_id': self.generated_model_id.id,
                'group_id': group.id,
                'perm_read': True,
                'perm_write': False,
                'perm_create': False,
                'perm_unlink': False,
            })
        company_line = self.field_ids.filtered(
            lambda line: line.kind == 'path' and line.path == 'company_id')
        if company_line:
            self.env['ir.rule'].sudo().create({
                'name': '%s: multi-company' % self.model_name,
                'model_id': self.generated_model_id.id,
                'domain_force': "[('%s', 'in', company_ids)]" % company_line[0].column_name,
            })

    def _ks_view_arch(self, view_type):
        """Build the pivot/list arch. ``kind='aggregate'`` columns get an
        ``avg="Label"`` footer/measure instead of ``sum`` - see the matching
        comment on ``IrModelFields._instanciate_attrs`` in ir_model.py for
        why 'sum' is always wrong for these (a repeated per-dimension lookup,
        not a per-row fact) and why 'avg' is the correct choice specifically
        when grouped by the same dimension the column is correlated on
        (which is the normal way to use one in a pivot). The list/pivot arch
        must agree with the field's own ``aggregator`` - declaring a
        different aggregator here than the field actually has raises errors
        client-side ("No aggregate function has been provided...").
        """
        self.ensure_one()
        numeric = self.field_ids.filtered(lambda line: line.ttype in KS_NUMERIC_TYPES)
        if view_type == 'list':
            columns = ''.join(
                '<field name="%s"%s/>' % (
                    line.column_name,
                    (' avg="%s"' if line.kind == 'aggregate' else ' sum="%s"') % line.label
                    if line in numeric else '')
                for line in self.field_ids)
            return '<list string="%s" create="false" edit="false">%s</list>' % (
                self.name, columns)
        rows = self.field_ids.filtered(
            lambda line: line.ttype not in KS_NUMERIC_TYPES)[:1]
        elements = ''.join(
            '<field name="%s" type="row"/>' % line.column_name for line in rows)
        elements += ''.join(
            '<field name="%s" type="measure"/>' % line.column_name for line in numeric)
        return '<pivot string="%s">%s</pivot>' % (self.name, elements)

    def _ks_create_ui(self):
        self.ensure_one()
        view_model = self.env['ir.ui.view'].sudo()
        views = view_model.browse()
        for view_type in ('pivot', 'list'):
            views |= view_model.create({
                'name': '%s.%s' % (self.model_name, view_type),
                'model': self.model_name,
                'type': view_type,
                'arch': self._ks_view_arch(view_type),
            })
        self.view_ids = views
        action = self.env['ir.actions.act_window'].sudo().create({
            'name': self.name,
            'res_model': self.model_name,
            'view_mode': 'pivot,list,graph',
            'domain': str(self._ks_translate_domain()),
            'view_ids': [
                Command.create({
                    'sequence': sequence,
                    'view_mode': view.type,
                    'view_id': view.id,
                })
                for sequence, view in enumerate(views)
            ],
        })
        self.action_id = action
        if self.create_menu and self.menu_parent_id:
            self.menu_id = self.env['ir.ui.menu'].sudo().create({
                'name': self.name,
                'parent_id': self.menu_parent_id.id,
                'action': 'ir.actions.act_window,%d' % action.id,
                'group_ids': [Command.set(self.group_ids.ids)] if self.group_ids else False,
            })

    def action_undeploy(self):
        for report in self:
            if report.menu_id:
                report.menu_id.sudo().unlink()
            if report.action_id:
                report.action_id.sudo().unlink()
            if report.view_ids:
                report.view_ids.sudo().unlink()
            if report.generated_model_id:
                # ir.model.unlink cascades fields, ACLs and record rules, and
                # reloads the registry itself.
                report.generated_model_id.sudo().unlink()
            report.write({'state': 'draft', 'generated_model_id': False,
                          'action_id': False, 'menu_id': False})
        return {"type":"ir.actions.client", "tag":"reload"}

    def action_open_report(self):
        self.ensure_one()
        if self.state != 'deployed' or not self.action_id:
            raise UserError(_("Deploy the report first."))
        return self.action_id.sudo()._get_action_dict()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def write(self, vals):
        structural = {'model_id', 'field_ids', 'skip_company_check'}
        if structural & set(vals):
            deployed = self.filtered(lambda report: report.state == 'deployed')
            if deployed:
                raise UserError(_(
                    "Undeploy %s before changing its base model or columns.",
                    ', '.join(deployed.mapped('name'))))
        return super().write(vals)

    def unlink(self):
        self.action_undeploy()
        return super().unlink()
