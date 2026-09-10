import ast
import logging

from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError

from .ir_model import KS_MODEL_PREFIX
from .ks_field_path_mixin import (
    KS_COMPANY_DEPENDENT_ERROR, ks_split_path, ks_walk_path, ks_x2many_sql_info,
)
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
    lang_id = fields.Many2one(
        comodel_name='res.lang',
        string='Report Language',
        domain="[('active', '=', True)]",
        default=lambda self: self.env['res.lang'].search(
            [('code', '=', self.env.lang or 'en_US')], limit=1),
        help="Language used to read translated text (product names, tag "
             "names...). A generated report is one stored SQL query shared by "
             "every user, so the language is fixed per report rather than "
             "following each viewer. Falls back to English (US) for records "
             "with no translation in this language.",
    )
    property_company_id = fields.Many2one(
        comodel_name='res.company',
        string='Report Company (Per-Company Fields)',
        help="Only needed to use a \"Per Company\" field (e.g. a category's "
             "Income Account) as a column or a correlation key. Such a field "
             "stores one value PER COMPANY, and a generated report is one "
             "stored SQL query shared by everyone - like Report Language for "
             "translated text, the company has to be fixed per report rather "
             "than following each viewer. Leave empty unless a column you "
             "need genuinely is a Per Company field; the error message when "
             "picking one names this setting.",
    )
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
    result_domain = fields.Char(
        string='Result Filter',
        default='[]',
        help="Filter on the report's OWN columns, including aggregate and "
             "computed ones - e.g. hide products with no activity. The "
             "ordinary Filter above is written against the base model and "
             "cannot see those columns. Only available once deployed, since "
             "the columns must exist before they can be filtered on. Note "
             "this hides rows AFTER the values are computed: it makes the "
             "report readable, it does not skip the work.",
    )
    deploy_warning = fields.Text(
        string='Performance Note', readonly=True, copy=False,
        help="Filled at deploy when something about this report will scale "
             "badly. Never blocks the deploy - the report is correct either "
             "way.")

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

    def _ks_register_join(self, joins, key, fk_expr, table):
        """``fk_expr`` is a complete, alias-qualified SQL expression for the
        join's left-hand side (e.g. ``base."categ_id"``, or the full
        unwrap-and-cast extraction for a "Per Company" field), not a bare
        column name. Callers build it via `_ks_field_sql_text`, so a
        company_dependent field works as a join hop, not just as a leaf.
        """
        if key not in joins:
            joins[key] = {
                'alias': 't%d' % (len(joins) + 1),
                'fk_expr': fk_expr,
                'table': table,
            }
        return joins[key]['alias']

    def _ks_lang_code(self):
        """Language used to read translated columns. See _ks_field_sql."""
        self.ensure_one()
        return self.lang_id.code or 'en_US'

    def _ks_field_sql(self, alias, column, field):
        """SQL reading one column, unwrapping a translated jsonb value.

        A translate=True field is stored as jsonb, not plain text, so
        selecting the bare column hands the generated field a dict the web
        client renders as "[object Object]". Odoo resolves the language in
        Python at read time, which a stored query cannot do, so the report
        picks one language explicitly (lang_id). en_US is the source language
        and always present, making it a safe fallback.
        """
        self.ensure_one()
        from odoo.tools.sql import SQL
        ident = SQL.identifier(alias, column)
        if getattr(field, 'company_dependent', False):
            return self._ks_company_dependent_sql(ident, field)
        if not getattr(field, 'translate', False):
            return ident
        lang = self._ks_lang_code()
        if lang == 'en_US':
            return SQL("%s->>%s", ident, 'en_US')
        return SQL("COALESCE(%s->>%s, %s->>%s)", ident, lang, ident, 'en_US')

    def _ks_company_dependent_sql(self, ident, field):
        """SQL reading a "Per Company" (company_dependent=True) column.

        Same jsonb storage as translate=True, but keyed by company id: the
        stored value is ``{"<company_id>": <value>}``, and for a Many2one that
        value is the plain integer id, so ``column->>'<id>'`` needs only a
        cast.

        Mirrors Odoo's fallback-aware ``Field.to_sql()``: a record with no
        explicit per-company override falls back to the company-level default
        in ``ir.default``, which is the common case rather than an edge case.
        The fallback is resolved for ``property_company_id`` and baked in as a
        literal - the same "per report, not per request" trade-off ``lang_id``
        makes, since one compiled query serves every viewer.
        """
        self.ensure_one()
        from odoo.tools.sql import SQL
        company = self.property_company_id
        if not company:
            raise UserError(KS_COMPANY_DEPENDENT_ERROR(field.name))
        raw = SQL("%s->>%s", ident, str(company.id))
        pg_type = field._column_type[1]
        fallback_sql = self._ks_company_dependent_fallback_sql(field, company)
        if fallback_sql is None:
            return SQL("(%s)::%s", raw, SQL(pg_type))
        # `raw` is text but the fallback is a real typed value, and COALESCE
        # requires both arms to agree - so cast the fallback to text first and
        # the whole result to the field's column type afterwards.
        return SQL("(COALESCE(%s, (%s)::text))::%s", raw, fallback_sql, SQL(pg_type))

    def _ks_company_dependent_fallback_sql(self, field, company):
        """The ``ir.default`` company-level fallback for a "Per Company"
        field, as a literal SQL fragment - or ``None`` if it cannot be
        resolved, in which case the caller behaves exactly as before this
        fix (no COALESCE, a missing value reads as NULL).

        Uses ``Field.get_company_dependent_fallback`` rather than querying
        ``ir.default`` by hand, so it stays correct if Odoo changes how
        defaults are cached.
        """
        self.ensure_one()
        from odoo.tools.sql import SQL
        try:
            scoped_model = self.env[field.model_name].with_company(company)
            fallback_value = field.get_company_dependent_fallback(scoped_model)
            if not fallback_value:
                # No default configured for this company - nothing to fall
                # back to.
                return None
            fallback_stored = field.convert_to_column(
                field.convert_to_write(fallback_value, scoped_model), scoped_model)
        except Exception:
            _logger.exception(
                "KS Report Builder: could not resolve the ir.default "
                "company fallback for %s on %s - reading it as NULL "
                "instead of erroring the whole report.",
                field.name, field.model_name)
            return None
        if fallback_stored is None:
            return None
        return SQL("%s", fallback_stored)

    def _ks_field_sql_text(self, alias, column, field):
        """_ks_field_sql as literal SQL text, for the outer query (which is
        assembled as a plain string - see _ks_build_query)."""
        sql = self._ks_field_sql(alias, column, field)
        return self.env.cr.mogrify(sql.code, sql.params).decode()

    def _ks_resolve_column(self, model_name, alias, field_name, joins, prefix):
        """Return (alias, column, field), following related/_inherits chains.

        Fields such as product.product/name physically live on product_template,
        and res.partner/country_code lives on res_country - in both cases the
        join to the real column is added automatically instead of failing. The
        Field itself is returned too because the caller needs it to know
        whether the column is a translated jsonb (see _ks_field_sql).

        ``field_name`` may be a DOTTED path, not just a single field: a
        ``related=`` chain can be longer than two segments (e.g.
        `account.move.country_code` =
        `company_id.account_fiscal_country_id.code`) and the delegation branch
        below hands the remainder back still dotted. Walking it hop by hop
        handles every depth rather than special-casing two.
        """
        if '.' in field_name:
            head, rest = field_name.split('.', 1)
            head_alias, head_column, head_field = self._ks_resolve_column(
                model_name, alias, head, joins, prefix)
            if head_field.type != 'many2one':
                raise UserError(_(
                    "Cannot drill through %(f)s (%(t)s) on %(m)s.",
                    f=head, t=head_field.type, m=model_name))
            target = self._ks_check_table_backed(head_field.comodel_name)
            key = prefix + head
            next_alias = self._ks_register_join(
                joins, key,
                self._ks_field_sql_text(head_alias, head_column, head_field),
                target._table)
            return self._ks_resolve_column(
                head_field.comodel_name, next_alias, rest, joins, key + '.')

        model = self._ks_check_table_backed(model_name)
        field = model._fields.get(field_name)
        if field is None:
            raise UserError(_("Field %(f)s does not exist on %(m)s.",
                              f=field_name, m=model_name))
        if getattr(field, 'company_dependent', False) and not self.property_company_id:
            # company_dependent fields are store=True, so the check below
            # would hand back their raw column - but that column is jsonb
            # keyed by company id, and joining it as an integer FK raises
            # `operator does not exist: integer = jsonb`. Rejected unless
            # property_company_id says which company's value to read; the
            # unwrapping itself happens in _ks_field_sql.
            raise UserError(KS_COMPANY_DEPENDENT_ERROR(field_name))
        if field.store:
            return alias, field.name, field
        # A related= field is a path in disguise and can be followed to the
        # column that really holds the value. Covers both _inherits delegation
        # and ordinary related fields, which fields_get exposes identically.
        if not field.related:
            raise UserError(_(
                "Field %(f)s on %(m)s is not stored and has no column to read.",
                f=field_name, m=model_name))
        parent_field_name, remainder = field.related.split('.', 1)
        parent_field = model._fields.get(parent_field_name)
        if parent_field is None:
            raise UserError(_("Field %(f)s does not exist on %(m)s.",
                              f=parent_field_name, m=model_name))
        if parent_field.type != 'many2one':
            # Following a related through a list would fan out rows.
            raise UserError(_(
                "Field %(f)s on %(m)s reads through %(p)s, which is a %(t)s "
                "and cannot be joined. Use a list column instead.",
                f=field_name, m=model_name, p=parent_field_name, t=parent_field.type))
        # resolve the parent itself first, so a chain of related fields works
        parent_alias, parent_column, resolved_parent_field = self._ks_resolve_column(
            model_name, alias, parent_field_name, joins, prefix)
        parent_model = self._ks_check_table_backed(parent_field.comodel_name)
        key = '%s@%s' % (prefix, parent_field_name)
        target_alias = self._ks_register_join(
            joins, key,
            self._ks_field_sql_text(parent_alias, parent_column, resolved_parent_field),
            parent_model._table)
        return self._ks_resolve_column(
            parent_field.comodel_name, target_alias, remainder, joins, key + '.')

    def _ks_resolve_path(self, path, joins):
        """Validate a dotted path and register every join it needs.

        Returns (alias, column, field) - the Field is needed by callers to
        build the read expression (a translated column is jsonb, not text).
        """
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
            resolved_alias, column, resolved_field = self._ks_resolve_column(
                model_name, alias, segment, joins, prefix)
            if index == len(segments) - 1:
                return resolved_alias, column, resolved_field
            if field.type != 'many2one':
                raise UserError(_(
                    "Cannot drill through %(f)s (%(t)s). Only many2one relations "
                    "can be followed.", f=segment, t=field.type))
            target = self._ks_check_table_backed(field.comodel_name)
            key = prefix + segment
            alias = self._ks_register_join(
                joins, key,
                self._ks_field_sql_text(resolved_alias, column, resolved_field),
                target._table)
            model_name = field.comodel_name
            prefix = key + '.'
        raise UserError(_("Could not resolve path %s.", path))

    # ------------------------------------------------------------------
    # Flatten a list (one2many/many2many) to one value per base row
    # ------------------------------------------------------------------

    def _ks_resolve_owner_alias(self, prefix, joins):
        """SQL alias of the table that OWNS a flattened list.

        ``base`` when the list sits on the report's own base model, otherwise
        the joined alias reached by ``prefix`` (a many2one-only path). The
        join key matches _ks_resolve_path's own convention, so a plain path
        column that already walked the same relation reuses the same alias
        instead of joining the table twice.
        """
        self.ensure_one()
        if not prefix:
            return 'base'
        alias, column, resolved_field = self._ks_resolve_path(prefix, joins)
        owner_field = ks_walk_path(
            self.env, self.model_id.model, prefix,
            allow_company_dependent=bool(self.property_company_id))
        owner_model = self._ks_check_table_backed(owner_field.comodel_name)
        return self._ks_register_join(
            joins, prefix,
            self._ks_field_sql_text(alias, column, resolved_field),
            owner_model._table)

    def _ks_query_column(self, query, model_name, alias, field_name):
        """(alias, column, field) for a field INSIDE a subquery, following
        related/_inherits chains - the Query-based twin of _ks_resolve_column.

        Separate from _ks_resolve_column because the subquery has its own
        FROM clause and alias namespace: joins are registered on the Query
        object (which the domain compiler also adds its joins to) rather than
        in the outer query's ``joins`` dict.

        Handles a dotted ``field_name`` for the same reason
        _ks_resolve_column does. The two must stay in step, or a field that
        works as a plain column breaks inside a flattened list.
        """
        if '.' in field_name:
            head, rest = field_name.split('.', 1)
            head_alias, head_column, head_field = self._ks_query_column(
                query, model_name, alias, head)
            if head_field.type != 'many2one':
                raise UserError(_(
                    "Cannot drill through %(f)s (%(t)s) inside a list.",
                    f=head, t=head_field.type))
            target = self._ks_check_table_backed(head_field.comodel_name)
            next_alias = query.left_join(
                head_alias, head_column, target._table, 'id', head)
            return self._ks_query_column(query, head_field.comodel_name, next_alias, rest)

        model = self._ks_check_table_backed(model_name)
        field = model._fields.get(field_name)
        if field is None:
            raise UserError(_("Field %(f)s does not exist on %(m)s.",
                              f=field_name, m=model_name))
        if getattr(field, 'company_dependent', False):
            # Rejected unconditionally here, unlike _ks_resolve_column:
            # property_company_id does not help. This subquery joins via
            # Query.left_join(), which takes a bare column name, not an
            # expression, so there is nowhere to splice the unwrap-and-cast
            # extraction in. A known residual gap.
            raise UserError(_(
                "%(f)s is a \"Per Company\" field, which cannot be used "
                "inside a flattened list even when 'Report Company' is set "
                "(only a plain column or an aggregate's correlation "
                "supports this).", f=field_name))
        if field.store:
            return alias, field.name, field
        # As in _ks_resolve_column: any related= field is a path to the
        # column that really holds the value.
        if not field.related:
            raise UserError(_(
                "Field %(f)s on %(m)s is not stored and has no column to "
                "read.", f=field_name, m=model_name))
        parent_field_name, remainder = field.related.split('.', 1)
        parent_field = model._fields.get(parent_field_name)
        if parent_field is None:
            raise UserError(_("Field %(f)s does not exist on %(m)s.",
                              f=parent_field_name, m=model_name))
        if parent_field.type != 'many2one':
            raise UserError(_(
                "Field %(f)s on %(m)s reads through %(p)s, which is a %(t)s "
                "and cannot be joined.",
                f=field_name, m=model_name, p=parent_field_name, t=parent_field.type))
        parent_alias, parent_column, _pf = self._ks_query_column(
            query, model_name, alias, parent_field_name)
        parent_model = self._ks_check_table_backed(parent_field.comodel_name)
        target_alias = query.left_join(
            parent_alias, parent_column, parent_model._table, 'id', parent_field_name)
        return self._ks_query_column(
            query, parent_field.comodel_name, target_alias, remainder)

    def _ks_resolve_query_path(self, query, model_name, alias, path):
        """Resolve a many2one-only path inside a subquery, adding its joins to
        ``query``. Returns (alias, column, field)."""
        segments = path.split('.')
        current_model, current_alias = model_name, alias
        for index, segment in enumerate(segments):
            model = self._ks_check_table_backed(current_model)
            field = model._fields.get(segment)
            if field is None:
                raise UserError(_("Field %(f)s does not exist on %(m)s.",
                                  f=segment, m=current_model))
            resolved_alias, column, resolved_field = self._ks_query_column(
                query, current_model, current_alias, segment)
            if index == len(segments) - 1:
                return resolved_alias, column, resolved_field
            if field.type != 'many2one':
                raise UserError(_(
                    "Cannot drill through %(f)s (%(t)s) inside a list.",
                    f=segment, t=field.type))
            target = self._ks_check_table_backed(field.comodel_name)
            current_alias = query.left_join(
                resolved_alias, column, target._table, 'id', segment)
            current_model = field.comodel_name
        raise UserError(_("Could not resolve path %s.", path))

    def _ks_resolve_display_name(self, query, m2o_field, alias, column):
        """Join a many2one's comodel and return its display column.

        Used when listing a relation as text: the readable value is the
        comodel's ``_rec_name`` (usually ``name``), not the FK id. Returns
        (alias, column, field) like the other resolvers.
        """
        comodel = self._ks_check_table_backed(m2o_field.comodel_name)
        rec_name = comodel._rec_name
        rec_field = comodel._fields.get(rec_name) if rec_name else None
        if rec_field is None or not (rec_field.store or getattr(rec_field, 'inherited', False)):
            raise UserError(_(
                "%(m)s has no stored name column, so its records cannot be "
                "listed as text. Drill one level further into a specific "
                "field of %(m)s instead.", m=m2o_field.comodel_name))
        target_alias = query.left_join(
            alias, column, comodel._table, 'id', m2o_field.name)
        return self._ks_query_column(
            query, m2o_field.comodel_name, target_alias, rec_name)

    def _ks_apply_flatten_hop_domain(self, query, hop_info, alias):
        """AND in one list hop's own ``domain=`` plus, for a one2many over a
        generic FK, Odoo's res_model-style discriminator.

        Not optional, and needed for every hop rather than just the first: a
        list over a generically keyed model (mail.message, ir.attachment)
        otherwise matches other models' rows that share a numeric id.
        """
        child_model = self._ks_check_table_backed(hop_info['child_model'])
        domain = hop_info['domain'].optimize_full(child_model)
        if not domain.is_true():
            query.add_where(domain._to_sql(child_model, alias, query))

    def _ks_field_domain_env(self):
        """Environment used to resolve a related field's OWN ``domain=`` when
        compiling a flatten subquery.

        A field's ``domain`` may be a CALLABLE, evaluated against whatever
        environment it is handed - including that env's active companies
        (``res.users.employee_ids`` is the live example). Compiled with the
        plain ``self.env``, that freezes the deploying user's company
        selection into the stored query, which is then served to everyone: the
        same report deployed by two people yields different SQL.

        The company context is therefore pinned deterministically to
        ``property_company_id`` when the report has one, otherwise to ALL
        companies - not the deployer's subset, because narrowing per-viewer
        data is the job of the generated model's multi-company ``ir.rule``,
        evaluated against each viewer's own companies at read time.
        """
        self.ensure_one()
        companies = self.property_company_id or self.env['res.company'].sudo().search([])
        return self.env(context=dict(self.env.context, allowed_company_ids=companies.ids))

    def _ks_resolve_query_owner_alias(self, query, model_name, alias, sub_path):
        """Alias, INSIDE a flatten subquery, of the table that owns the next
        list in a multi-hop chain.

        ``sub_path`` is the many2one path to walk after landing on the
        previous hop's comodel (empty when the next list is defined directly
        on it). The subquery twin of _ks_resolve_owner_alias, which does the
        same job against the outer query's ``joins`` dict.
        """
        if not sub_path:
            return alias
        resolved_alias, column, field = self._ks_resolve_query_path(
            query, model_name, alias, sub_path)
        if field.type != 'many2one':
            raise UserError(_(
                "Cannot drill through %(f)s (%(t)s) to reach the next list.",
                f=field.name, t=field.type))
        target = self._ks_check_table_backed(field.comodel_name)
        return query.left_join(resolved_alias, column, target._table, 'id', field.name)

    def _ks_build_flatten_subquery(self, line, info, joins):
        """Return SQL collapsing one or more one2many/many2many hops down to
        ONE value.

        Like the aggregate column, a correlated subquery rather than a join:
        the child list is reduced to one scalar inside its own parentheses, so
        the outer query still returns one row per base record and nothing is
        double counted.

        A path may cross several lists (``milestone_ids.task_ids.name``).
        Each extra hop is another JOIN inside this same subquery, so the chain
        fans out only in here and the single aggregate collapses it again
        before anything escapes the parentheses - no nested collapsing needed.

        The last hop keeps the alias ``t`` and hop 0 keeps ``ks_rel`` for a
        many2many relation table, so a single-hop path emits the same SQL as
        it did before multi-hop support, making "no existing report changed" a
        checkable test.
        """
        self.ensure_one()
        from odoo.orm.domains import Domain
        from odoo.tools.query import Query
        from odoo.tools.sql import SQL

        hops = info['hops']
        if not hops:
            raise UserError(_("Path %s does not go through a list.", line.path))
        last_index = len(hops) - 1
        aliases = [
            't' if index == last_index else 't%d' % index
            for index in range(len(hops))
        ]

        domain_env = self._ks_field_domain_env()

        query = None
        for index, hop in enumerate(hops):
            # Not self.env: a callable field domain resolved against the
            # deploying session would bake that user's active companies into
            # the stored query. See _ks_field_domain_env.
            hop_info = ks_x2many_sql_info(domain_env, hop['field'], hop['owner_model'])
            alias = aliases[index]
            rel_alias = 'ks_rel' if index == 0 else 'ks_rel_%d' % index

            if index == 0:
                # Correlate the first list back to the OUTER query's row.
                owner_alias = self._ks_resolve_owner_alias(info['prefix'], joins)
                query = Query(self.env, alias, SQL.identifier(hop_info['child_table']))
                if hop_info['mode'] == 'one2many':
                    query.add_where(SQL(
                        "%s = %s",
                        SQL.identifier(alias, hop_info['inverse_column']),
                        SQL.identifier(owner_alias, 'id')))
                else:
                    # many2many: the relation table sits between owner and child.
                    query.add_join(
                        'JOIN', rel_alias, SQL.identifier(hop_info['rel_table']),
                        SQL("%s = %s",
                            SQL.identifier(rel_alias, hop_info['rel_child_column']),
                            SQL.identifier(alias, 'id')))
                    query.add_where(SQL(
                        "%s = %s",
                        SQL.identifier(rel_alias, hop_info['rel_owner_column']),
                        SQL.identifier(owner_alias, 'id')))
            else:
                # Later lists join to the previous one inside the subquery.
                # INNER, deliberately: a parent with no children must
                # contribute nothing, or COUNT(*) would count it as one.
                previous = hops[index - 1]
                parent_alias = self._ks_resolve_query_owner_alias(
                    query, previous['field'].comodel_name, aliases[index - 1],
                    previous['sub_path'])
                if hop_info['mode'] == 'one2many':
                    query.add_join(
                        'JOIN', alias, SQL.identifier(hop_info['child_table']),
                        SQL("%s = %s",
                            SQL.identifier(alias, hop_info['inverse_column']),
                            SQL.identifier(parent_alias, 'id')))
                else:
                    query.add_join(
                        'JOIN', rel_alias, SQL.identifier(hop_info['rel_table']),
                        SQL("%s = %s",
                            SQL.identifier(rel_alias, hop_info['rel_owner_column']),
                            SQL.identifier(parent_alias, 'id')))
                    query.add_join(
                        'JOIN', alias, SQL.identifier(hop_info['child_table']),
                        SQL("%s = %s",
                            SQL.identifier(rel_alias, hop_info['rel_child_column']),
                            SQL.identifier(alias, 'id')))

            self._ks_apply_flatten_hop_domain(query, hop_info, alias)

        leaf_alias = aliases[last_index]
        leaf_model_name = hops[last_index]['field'].comodel_name
        child_model = self._ks_check_table_backed(leaf_model_name)

        # User filter on the child rows, written against the LAST list's
        # model - the flatten twin of agg_domain.
        user_domain = ast.literal_eval(line.flat_domain or '[]')
        if user_domain:
            compiled = Domain(user_domain).optimize_full(child_model)
            query.add_where(compiled._to_sql(child_model, leaf_alias, query))

        collapse = line.collapse
        if collapse == 'count':
            if last_index == 0:
                measure = SQL("COUNT(*)")
            else:
                # Across several hops the same row can be reached more than
                # once (notably through a many2many), so COUNT(*) would count
                # paths rather than records.
                measure = SQL("COUNT(DISTINCT %s)", SQL.identifier(leaf_alias, 'id'))
        else:
            value_alias, value_column, value_field = self._ks_resolve_query_path(
                query, leaf_model_name, leaf_alias, info['suffix'])
            if collapse in ('list', 'list_all') and value_field.type == 'many2one':
                # Follow the relation one more hop to the comodel's
                # _rec_name, so the text reads as names rather than raw ids.
                value_alias, value_column, value_field = \
                    self._ks_resolve_display_name(query, value_field, value_alias, value_column)
            value = self._ks_field_sql(value_alias, value_column, value_field)
            if collapse in ('list', 'list_all'):
                # ORDER BY makes the string deterministic. array_agg +
                # array_to_string rather than string_agg(x::text) so both the
                # de-duplication and the ordering happen on the REAL value:
                # PostgreSQL requires an aggregate's ORDER BY to match its
                # DISTINCT expression, so casting inside string_agg would sort
                # numbers lexically ("10, 12, 15, 7").
                #
                # DISTINCT is the only difference between the two list modes,
                # and it de-duplicates on the displayed TEXT, not the record -
                # so two records sharing a name collapse into one entry while
                # a Count over the same path still counts both. 'list_all'
                # keeps every record, so entries always tally with a Count.
                distinct = SQL("DISTINCT ") if collapse == 'list' else SQL("")
                measure = SQL(
                    "array_to_string(array_agg(%s%s ORDER BY %s), ', ')",
                    distinct, value, value)
            else:
                measure = value
                direction = SQL("ASC") if collapse == 'first' else SQL("DESC")
                order_parts = []
                # Order by the whole chain, outermost list first: "first"
                # means the first task of the first milestone.
                for index, hop in enumerate(hops):
                    hop_model = self._ks_check_table_backed(hop['field'].comodel_name)
                    order_field = hop_model._fields.get('sequence')
                    if order_field is not None and order_field.store:
                        order_parts.append(SQL(
                            "%s %s", SQL.identifier(aliases[index], 'sequence'), direction))
                    order_parts.append(SQL(
                        "%s %s", SQL.identifier(aliases[index], 'id'), direction))
                query.order = SQL(", ").join(order_parts)
                query.limit = 1

        subselect = query.subselect(measure)
        return self.env.cr.mogrify(subselect.code, subselect.params).decode()

    # ------------------------------------------------------------------
    # Aggregate lookup (scalar correlated subquery over an unrelated table)
    # ------------------------------------------------------------------

    def _ks_build_aggregate_subquery(self, line, joins):
        """Return SQL for a scalar SUM/COUNT/AVG/MAX/MIN over ``agg_model_id``,
        correlated back to this row on a shared many2one dimension.

        A correlated subquery, not a join: the child table collapses to one
        number inside its own parentheses, so the outer query stays one row
        per base record with no GROUP BY. The correlation key is resolved
        through _ks_resolve_path, so multi-hop correlation reuses the same
        joins as any other column.

        ``agg_domain``, if set, is written against agg_model_id's own fields
        and restricts which child rows are aggregated. A domain leaf drilling
        through a relation needs its own JOIN inside the subquery, and Odoo's
        domain compiler registers that join on the ``Query`` it is given - so
        the subquery MUST be built through that same ``Query`` (its
        ``subselect()``), never hand-written as a bare "FROM table t", or the
        compiled WHERE references an alias with no FROM-clause entry.
        """
        self.ensure_one()
        from odoo.orm.domains import Domain
        from odoo.tools.query import Query
        from odoo.tools.sql import SQL

        if line.agg_base_path:
            base_alias, base_column, __ = self._ks_resolve_path(line.agg_base_path, joins)
        else:
            # correlate on the base record itself - see _check_aggregate
            base_alias, base_column = 'base', 'id'
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
        the path columns, not inline: a SELECT-list alias is not visible to a
        sibling expression in the same list, so referencing "x_quantity" there
        fails with "column does not exist". Wrapping makes it a real output
        column of the derived table.
        """
        self.ensure_one()
        joins = {}
        path_selections = []
        numeric_columns = []

        # Aggregate columns resolve alongside path columns, not in the outer
        # expression layer: a correlated subquery references base/join aliases
        # directly and never needs a sibling column's alias.
        inner_lines = self.field_ids.filtered(lambda line: line.kind in ('path', 'aggregate'))
        for line in inner_lines:
            if line.kind == 'path':
                # A path crossing a list is collapsed to one value by a
                # correlated subquery; a plain many2one path is a direct
                # column read off base or a joined table.
                info = ks_split_path(
                    self.env, self.model_id.model, line.path,
                    allow_company_dependent=bool(self.property_company_id))
                if info['hops']:
                    selection_sql = self._ks_build_flatten_subquery(line, info, joins)
                    path_selections.append('%s AS "%s"' % (selection_sql, line.column_name))
                else:
                    alias, column, field = self._ks_resolve_path(line.path, joins)
                    path_selections.append('%s AS "%s"' % (
                        self._ks_field_sql_text(alias, column, field),
                        line.column_name))
            else:
                selection_sql = self._ks_build_aggregate_subquery(line, joins)
                path_selections.append('%s AS "%s"' % (selection_sql, line.column_name))
            if line.ttype in KS_NUMERIC_TYPES:
                numeric_columns.append(line.column_name)

        base_table = self._ks_check_table_backed(self.model_id.model)._table
        join_sql = ''.join(
            # fk_expr is already a complete, alias-qualified expression (see
            # _ks_register_join), so it is spliced in as-is rather than
            # re-quoted as a bare column name.
            ' LEFT JOIN "%(table)s" %(alias)s ON %(alias)s.id = %(fk)s' % {
                'table': join['table'],
                'alias': join['alias'],
                'fk': join['fk_expr'],
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

    @api.constrains('result_domain')
    def _check_result_domain(self):
        """Validate the Result Filter against the GENERATED model.

        Only possible once deployed - before that the columns it refers to do
        not exist yet, which is also why the field is hidden in draft.

        Deliberately does NOT watch ``state``: action_deploy calls
        action_undeploy first, which writes state='draft', so watching it
        fired this error against a filter legitimately set while deployed and
        made such a report impossible to redeploy. Watching only
        ``result_domain`` still catches the case this exists for.
        """
        from odoo.orm.domains import Domain
        for report in self:
            raw = report.result_domain
            if not raw or raw == '[]':
                continue
            try:
                parsed = ast.literal_eval(raw)
                assert isinstance(parsed, list)
            except (ValueError, SyntaxError, AssertionError) as error:
                raise ValidationError(
                    _("Result Filter must be a valid domain list.")) from error
            if report.state != 'deployed' or not report.model_name:
                raise ValidationError(_(
                    "Deploy %s before setting a Result Filter - it filters on "
                    "the report's own generated columns, which only exist "
                    "once the report is deployed.", report.name))
            model = report.env.get(report.model_name)
            if model is None:
                continue
            try:
                Domain(parsed).optimize_full(model)
            except Exception as error:
                raise ValidationError(_(
                    "Invalid Result Filter (%s).", str(error))) from error

    def _ks_translate_domain(self):
        """Rewrite the Filter from base-model field paths to generated columns.

        The Filter is written against the BASE model (the domain widget needs
        a real model to offer field choices) but ends up on the GENERATED
        model's action, which only has the x_-prefixed columns this report
        declared. A leaf can only be translated if its path exactly matches
        one of the report's own "path" columns.
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
        warnings = []
        for report in self:
            report._ks_validate()
            report.action_undeploy()
            query = report._ks_build_query()
            model_name = report._ks_model_name()
            report.write({'model_name': model_name, 'query': query})
            # The query is read back with raw SQL during registry setup,
            # which happens inside ir.model.create below.
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
            try:
                warnings += report._ks_check_correlation_indexes()
            except Exception:  # noqa: BLE001 - a diagnostic must never break deploy
                _logger.exception(
                    "KS Report Builder: index check failed for %s", report.name)
        # Always reload: deploy creates the menu item and the web client only
        # picks it up on a reload. The warning is persisted on the record and
        # shown on the form instead, where it survives that reload.
        return {"type": "ir.actions.client", "tag": "reload"}

    def _ks_check_correlation_indexes(self):
        """Warn when an aggregate's link column has no index.

        Every aggregate column runs its correlated subquery once per report
        row, so the link column is looked up as many times as there are rows.
        With an index that stays linear; without one PostgreSQL falls back to
        a full scan per row.

        A warning, not a blocker: on a small table Postgres legitimately
        prefers a sequential scan even when an index exists, and the report is
        correct either way - only the growth curve suffers.
        """
        self.ensure_one()
        unindexed = []
        for line in self.field_ids.filtered(lambda ln: ln.kind == 'aggregate'):
            if not (line.agg_model_id and line.agg_link_field_id):
                continue
            model = self.env.get(line.agg_model_id.model)
            if model is None or not model._auto:
                continue
            column = line.agg_link_field_id.name
            self.env.cr.execute("""
                SELECT 1 FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_attribute a
                    ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
                 WHERE c.relname = %s AND a.attname = %s
                 LIMIT 1
            """, (model._table, column))
            if not self.env.cr.fetchone():
                unindexed.append('%s.%s' % (model._table, column))
        if unindexed:
            _logger.warning(
                "KS Report Builder: report %s aggregates on un-indexed "
                "column(s) %s - each is scanned once per report row. Consider "
                "an index if the source table is large.",
                self.name, ', '.join(unindexed))
            self.deploy_warning = _(
                "These aggregate link columns have no database index: %s.\n"
                "Each one is looked up once per report row, so this only "
                "matters if those tables are large. The report is correct "
                "either way.", ', '.join(sorted(set(unindexed))))
        else:
            self.deploy_warning = False
        return unindexed

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
            # '|' with company_id=False mirrors Odoo's own multi-company rule
            # convention: a record with no company is shared across all
            # companies, not invisible to all of them. Without that branch
            # 'in' matches nothing for such rows, silently emptying the whole
            # report on models where they are common.
            column_name = company_line[0].column_name
            self.env['ir.rule'].sudo().create({
                'name': '%s: multi-company' % self.model_name,
                'model_id': self.generated_model_id.id,
                'domain_force': "['|', ('%s', '=', False), ('%s', 'in', company_ids)]" % (
                    column_name, column_name),
            })

    def _ks_view_arch(self, view_type):
        """Build the pivot/list arch.

        ``kind='aggregate'`` columns get ``avg="Label"`` instead of ``sum``
        only when they correlate on a shared dimension (agg_base_path set); a
        self-correlated one is a genuine per-row total and keeps ``sum``. This
        mirrors IrModelFields._instanciate_attrs, and must: an arch declaring
        a different aggregator than the field itself carries raises "No
        aggregate function has been provided" client-side.
        """
        self.ensure_one()
        numeric = self.field_ids.filtered(lambda line: line.ttype in KS_NUMERIC_TYPES)
        if view_type == 'search':
            # Without a generated search view the report opens with an empty
            # search bar and no Group By entries.
            parts = ''.join(
                '<field name="%s"/>' % line.column_name
                for line in self.field_ids.filtered('searchable'))
            groups = ''.join(
                '<filter name="ks_group_%(col)s" string="%(label)s"'
                ' context="{\'group_by\': \'%(col)s\'}"/>' % {
                    'col': line.column_name,
                    'label': (line.label or line.column_name).replace('"', "'"),
                }
                for line in self.field_ids.filtered('groupable'))
            if groups:
                # A bare <group> is required in v19: the search-view RNG
                # rejects string="" and expand="" on it, failing as the
                # generic "Invalid view ... definition" with no detail.
                groups = '<group>%s</group>' % groups
            return '<search string="%s">%s%s</search>' % (self.name, parts, groups)
        if view_type == 'list':
            columns = ''.join(
                '<field name="%s"%s/>' % (
                    line.column_name,
                    (' avg="%s"'
                     if (line.kind == 'aggregate' and line.agg_base_path)
                     else ' sum="%s"') % line.label
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

    def _ks_action_domain(self):
        """The action's domain: the base-model Filter (translated to generated
        column names) AND the Result Filter (already written against the
        generated model, so it needs no translation)."""
        self.ensure_one()
        base = self._ks_translate_domain()
        result = ast.literal_eval(self.result_domain or '[]')
        if not result:
            return base
        if not base:
            return result
        return ['&'] + list(base) + list(result)

    def _ks_sync_action_domain(self):
        """Push a Filter change onto the live action without a redeploy.

        Both filters live only on the action, never in the stored SQL, so
        changing one needs no new query, model or views.
        """
        for report in self:
            if report.state == 'deployed' and report.action_id:
                report.action_id.sudo().domain = str(report._ks_action_domain())

    def _ks_create_ui(self):
        self.ensure_one()
        view_model = self.env['ir.ui.view'].sudo()
        views = view_model.browse()
        for view_type in ('pivot', 'list', 'search'):
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
            'domain': str(self._ks_action_domain()),
            'search_view_id': views.filtered(lambda v: v.type == 'search')[:1].id or False,
            'view_ids': [
                Command.create({
                    'sequence': sequence,
                    'view_mode': view.type,
                    'view_id': view.id,
                })
                # the search view is attached via search_view_id above; listing
                # it here too would make it a display mode of the action
                for sequence, view in enumerate(views.filtered(lambda v: v.type != 'search'))
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

    def action_open_designer(self):
        """Reopen this report's columns on the visual canvas (the ER-diagram
        style builder), instead of the plain Columns list.

        Passed as a client-action param: ks_load_report reads it directly and
        runs the "can this be represented on a canvas" checks. Deliberately
        does not pre-check state=='deployed' - the designer's Save calls
        ks_update_report, which raises the same "Undeploy first" message
        write() already enforces, so the rule lives in one place.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'ks_report_designer',
            'params': {'report_id': self.id},
        }

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
        result = super().write(vals)
        if {'domain', 'result_domain'} & set(vals):
            self._ks_sync_action_domain()
        if {'menu_parent_id', 'create_menu', 'name', 'group_ids'} & set(vals):
            self._ks_sync_menu()
        return result

    def _ks_sync_menu(self):
        """Push a Menu Location/Create Menu Entry/name/Visible To change onto
        the live menu without a redeploy - the twin of _ks_sync_action_domain.

        _ks_create_ui() only runs at Deploy time, so without this, editing
        "Menu Location" on a deployed report updates the field but leaves the
        real ir.ui.menu parented where it was created - the change appears to
        save while nothing moves until an Undeploy/Deploy round trip.
        """
        menu_model = self.env['ir.ui.menu'].sudo()
        for report in self:
            if report.state != 'deployed':
                continue
            if not report.create_menu or not report.menu_parent_id:
                # Turning the entry off, or clearing its location, removes
                # the menu outright - there is nowhere left for it to live.
                if report.menu_id:
                    report.menu_id.sudo().unlink()
                    report.menu_id = False
                continue
            group_ids = [Command.set(report.group_ids.ids)] if report.group_ids else False
            if report.menu_id:
                report.menu_id.sudo().write({
                    'name': report.name,
                    'parent_id': report.menu_parent_id.id,
                    'group_ids': group_ids,
                })
            elif report.action_id:
                # Create Menu Entry / Menu Location was off (or unset) at
                # Deploy time and is only now being turned on.
                report.menu_id = menu_model.create({
                    'name': report.name,
                    'parent_id': report.menu_parent_id.id,
                    'action': 'ir.actions.act_window,%d' % report.action_id.id,
                    'group_ids': group_ids,
                })

    def unlink(self):
        self.action_undeploy()
        return super().unlink()
