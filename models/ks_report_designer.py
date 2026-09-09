"""Backend for the visual (ER-diagram style) report designer.

Deliberately a THIN layer: the designer is an alternative *authoring UI*, not a
second reporting engine. Everything it produces is an ordinary
``ks.report.builder`` record with ordinary ``ks.report.builder.field`` lines,
so path resolution, list flattening, aggregates, translated columns, the
deploy pipeline and every validation rule already tested elsewhere apply to it
unchanged. Nothing here duplicates the query builder.
"""

import datetime
import decimal

from odoo import Command, api, models, _
from odoo.exceptions import UserError

from .ir_model import KS_MODEL_PREFIX
from .ks_report_builder_field import KS_AGGREGATE_NUMERIC_TYPES
from .ks_field_path_mixin import (
    KS_SCALAR_TYPES, KS_TRAVERSABLE_TYPES, KS_X2MANY_TYPES, ks_split_path,
)

# Fields that are noise on a canvas: every model has them and nobody reports
# on them by choice. Hidden by default, still reachable via "show technical".
KS_NOISE_FIELDS = {
    'create_uid', 'create_date', 'write_uid', 'write_date', '__last_update',
    'display_name', 'id',
}


class KsReportDesigner(models.AbstractModel):
    _name = 'ks.report.designer'
    _description = 'Visual Report Designer'

    # ------------------------------------------------------------------
    # Schema introspection for the canvas
    # ------------------------------------------------------------------

    @api.model
    def ks_search_models(self, query, limit=20):
        """Find candidate base models, ranked by relevance.

        A plain ``name ilike <query>`` ordered by name is unusable here, as
        found live: searching "sale" returned six alphabetically-first models
        and `sale.order` was not among them, and "sale order" matched
        "Point of Sale Orders Lines" while MISSING "Sales Order" (not a
        contiguous substring). So the query is tokenised - every word must
        match somewhere - and the results are then ranked, exact/prefix
        matches first, shorter model names before their many derived
        cousins (`sale.order` before `sale.order.template.line`).
        """
        query = (query or '').strip()
        if len(query) < 2:
            return []
        tokens = [t for t in query.replace('.', ' ').split() if t]
        domain = [('transient', '=', False), ('abstract', '=', False),
                  ('model', 'not like', '%s%%' % KS_MODEL_PREFIX)]
        for token in tokens:
            domain += ['|', ('name', 'ilike', token), ('model', 'ilike', token)]
        records = self.env['ir.model'].search_read(domain, ['model', 'name'], limit=300)

        needle = query.lower()
        flat = needle.replace(' ', '').replace('.', '')

        def rank(rec):
            model = (rec['model'] or '').lower()
            name = (rec['name'] or '').lower()
            if model == needle or model.replace('.', '') == flat:
                return 0
            if name == needle:
                return 1
            if model.startswith(needle) or model.replace('.', '').startswith(flat):
                return 2
            if name.startswith(needle):
                return 3
            # a deeper dotted name is almost always a satellite of the model
            # the user actually meant
            return 4 + model.count('.')

        records.sort(key=lambda r: (rank(r), len(r['model'] or ''), r['model'] or ''))
        return records[:limit]

    @api.model
    def ks_model_schema(self, model_name, include_technical=False):
        """Describe one model for a canvas node.

        Returns the fields split into what can be dragged out as a column and
        what can be expanded into another node. The rules mirror the field
        chain picker exactly (ks_field_chain_field.js / ks_walk_path) so the
        canvas can never offer something the server would later reject.
        """
        model = self.env.get(model_name)
        if model is None:
            raise UserError(_("Model %s is not present in the registry.", model_name))
        if not model._auto:
            raise UserError(_(
                "%s is not backed by a real table, so it cannot be used here.",
                model_name))

        columns, relations = [], []
        for name, field in sorted(model._fields.items(), key=lambda kv: kv[0]):
            if not include_technical and name in KS_NOISE_FIELDS:
                continue
            if field.groups and not self.env.user.has_groups(field.groups):
                continue
            readable = bool(field.store) or bool(field.related)
            info = {
                'name': name,
                'string': field.string or name,
                'ttype': field.type,
                'relation': field.comodel_name if field.relational else False,
                'translate': bool(getattr(field, 'translate', False)),
            }
            if field.type in KS_SCALAR_TYPES and readable:
                columns.append(info)
            if field.type in KS_TRAVERSABLE_TYPES and readable:
                relations.append(dict(info, kind='many2one'))
            elif field.type in KS_X2MANY_TYPES and field.store:
                # A list can be expanded, but anything dragged out of it (or
                # out of a node beyond it) needs a collapse - see
                # ks_split_path / the `collapse` field.
                relations.append(dict(info, kind='x2many'))
        return {
            'model': model_name,
            'name': model._description or model_name,
            'columns': columns,
            'relations': relations,
        }

    # ------------------------------------------------------------------
    # Aggregate columns (kind='aggregate') from the canvas
    # ------------------------------------------------------------------

    @api.model
    def ks_aggregate_sources(self, dimension_model, query='', limit=25):
        """Tables that can be aggregated against ``dimension_model``.

        An aggregate column correlates some other table T back to a dimension
        D through a many2one on T (see `_check_aggregate`). Rather than make
        the user pick any model and discover at save time that it has no link
        to D - which is what the Report Definition form does - only tables
        that genuinely have a stored many2one to D AND at least one stored
        numeric field are offered here.
        """
        links = self.env['ir.model.fields'].search_read(
            [('ttype', '=', 'many2one'), ('relation', '=', dimension_model),
             ('store', '=', True)],
            ['model'], limit=2000)
        candidates = sorted({link['model'] for link in links})
        if not candidates:
            return []
        # COUNT is meaningful without a numeric column, but every other
        # aggregator needs one, so a table with no measure is nearly useless.
        measures = self.env['ir.model.fields'].search_read(
            [('model', 'in', candidates),
             ('ttype', 'in', list(KS_AGGREGATE_NUMERIC_TYPES)),
             ('store', '=', True)],
            ['model'], limit=5000)
        usable = {f['model'] for f in measures}
        domain = [('model', 'in', sorted(usable)),
                  ('transient', '=', False), ('abstract', '=', False),
                  ('model', 'not like', '%s%%' % KS_MODEL_PREFIX)]
        query = (query or '').strip()
        for token in query.replace('.', ' ').split():
            domain += ['|', ('name', 'ilike', token), ('model', 'ilike', token)]
        records = self.env['ir.model'].search_read(domain, ['model', 'name'], limit=300)
        if query:
            needle = query.lower()
            # dots and spaces are interchangeable to a typing user: without
            # this "stock quant" ranked report.stock.quantity above the
            # stock.quant they meant (same bug ks_search_models already fixed)
            flat = needle.replace(' ', '').replace('.', '')

            def rank(rec):
                model = (rec['model'] or '').lower()
                name = (rec['name'] or '').lower()
                if model == needle or model.replace('.', '') == flat:
                    return 0
                if model.startswith(needle) or model.replace('.', '').startswith(flat):
                    return 1
                if name.startswith(needle):
                    return 2
                return 3 + model.count('.')

            records.sort(key=lambda r: (rank(r), len(r['model'] or ''), r['model'] or ''))
        else:
            records.sort(key=lambda r: (r['name'] or '').lower())
        return records[:limit]

    @api.model
    def ks_aggregate_fields(self, model_name, dimension_model):
        """The link fields and measures selectable for one aggregate source.

        Mirrors the domains on `agg_link_field_id` / `agg_measure_field_id`
        and the `_check_aggregate` constraint, so the canvas can only build
        combinations the server will accept.
        """
        fields_model = self.env['ir.model.fields']
        links = fields_model.search_read(
            [('model', '=', model_name), ('ttype', '=', 'many2one'),
             ('relation', '=', dimension_model), ('store', '=', True)],
            ['id', 'name', 'field_description'])
        measures = fields_model.search_read(
            # 'id' is numeric and stored but aggregating a primary key is
            # meaningless - and it sorted first as "ID", so it became the
            # default measure and produced "SUM ID" labels.
            [('model', '=', model_name),
             ('ttype', 'in', list(KS_AGGREGATE_NUMERIC_TYPES)),
             ('store', '=', True), ('name', '!=', 'id')],
            ['id', 'name', 'field_description', 'ttype'])
        measures.sort(key=lambda f: (f['field_description'] or '').lower())
        return {'links': links, 'measures': measures}

    # ------------------------------------------------------------------
    # Turning a canvas into a real report definition
    # ------------------------------------------------------------------

    @api.model
    def ks_preview_path(self, base_model, path, collapse=None):
        """Validate one candidate column and say how it will behave.

        Called as the user drops a field, so the canvas can show the problem
        immediately (and ask for a collapse when the path crosses a list)
        rather than failing later at save or deploy time.
        """
        try:
            info = ks_split_path(self.env, base_model, path)
        except Exception as error:
            return {'ok': False, 'error': str(error)}
        crosses_list = info['x2many'] is not None
        return {
            'ok': True,
            'crosses_list': crosses_list,
            'list_model': info['x2many'].comodel_name if crosses_list else False,
            'terminal_is_list': crosses_list and info['field'] is None,
            'needs_collapse': crosses_list and not collapse,
        }

    def _ks_column_vals(self, columns):
        """Shared by ks_create_report and ks_preview: canvas column dicts to
        ks.report.builder.field vals, so the preview a user sees is built by
        EXACTLY the same mapping that Create Report will persist - a
        preview that could show something different from the real report
        would be worse than no preview at all."""
        lines = []
        for index, col in enumerate(columns):
            kind = col.get('kind') or 'path'
            vals = {
                'kind': kind,
                'sequence': (index + 1) * 10,
                'label': col.get('label') or col.get('path') or _('Column'),
            }
            if kind == 'aggregate':
                # The canvas supplies ids it got from ks_aggregate_fields, so
                # these land on the same constraints the form's own aggregate
                # columns go through (_check_aggregate).
                vals.update({
                    'agg_base_path': col.get('agg_base_path'),
                    'agg_model_id': col.get('agg_model_id'),
                    'agg_link_field_id': col.get('agg_link_field_id'),
                    'agg_measure_field_id': col.get('agg_measure_field_id'),
                    'agg_function': col.get('agg_function'),
                })
                if col.get('agg_domain'):
                    vals['agg_domain'] = col['agg_domain']
            else:
                vals['path'] = col.get('path')
                if col.get('collapse'):
                    vals['collapse'] = col['collapse']
                if col.get('flat_domain'):
                    vals['flat_domain'] = col['flat_domain']
            lines.append(vals)
        return lines

    @api.model
    def ks_preview(self, payload, limit=8):
        """Run the canvas's query for real and return a sample, WITHOUT
        creating anything - no ir.model, no ks.report.builder record, no
        database write at all.

        ``limit`` defaults to 8 for the sidebar strip; the "expand" dialog
        asks for more (see KsReportDesigner.openPreviewDialog). Clamped
        server-side rather than trusted from the client - this runs before
        the report (and its access rules) exist, so nothing should let a
        caller demand an arbitrarily large scan.

        Built via ``env.new()``: an in-memory, unsaved ks.report.builder
        whose computed fields (column_name, ttype...) evaluate exactly as
        they would on a real record, so it can be handed straight to the
        real ``_ks_build_query()`` - no second query builder to keep in sync
        with the first. ``@api.constrains`` do not run on a new() record
        (those only fire on write/create), but that is not a gap here: every
        check that actually matters for building SQL (unknown field, wrong
        type, list without a collapse...) is enforced by _ks_build_query's
        own resolvers raising UserError, the same as at Deploy time - the
        constrains are extra, save-time politeness on top of that, not the
        only guard.
        """
        model_name = (payload or {}).get('model')
        columns = (payload or {}).get('columns') or []
        limit = max(1, min(int(limit or 8), 200))
        if not model_name or not columns:
            return {'ok': False, 'error': _("Pick a base model and add at least one column.")}
        model_rec = self.env['ir.model'].search([('model', '=', model_name)], limit=1)
        if not model_rec:
            return {'ok': False, 'error': _("Model %s does not exist.", model_name)}

        report = self.env['ks.report.builder'].new({
            'name': payload.get('name') or _('Preview'),
            'model_id': model_rec.id,
            'lang_id': payload.get('lang_id') or False,
            'field_ids': [Command.create(vals) for vals in self._ks_column_vals(columns)],
        })
        try:
            query = report._ks_build_query()
        except Exception as error:  # noqa: BLE001 - surfaced to the user, not a bug report
            return {'ok': False, 'error': str(error)}

        # NB: the report-level Filter/Result Filter are not applied here.
        # Both are set on the real ks.report.builder record after creation
        # (Result Filter specifically requires a deployed model to validate
        # against - see _check_result_domain), so the designer never has one
        # to preview with. This shows every row the query itself produces.
        # ks_preview runs raw SQL, not the ORM's read(), so a many2one column
        # comes back as a bare id - resolved to a display name below, since a
        # column of ids ("9", "11", "9") is not what "preview the data" means
        # to a user, especially for a many2one reached through a flattened
        # First/Last (kind='path' with collapse, still a real many2one).
        relation_by_col = {
            line.column_name: line.relation
            for line in report.field_ids if line.relation
        }
        cols = [(line.column_name, line.label, line.ttype) for line in report.field_ids]
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT COUNT(*) FROM (%s) ks_preview_c' % query)
                total = self.env.cr.fetchone()[0]
                # `limit` is a clamped Python int (validated above), not user
                # text, so it is safe to format in directly - this module's
                # whole query engine is built as one fully-literal SQL string
                # (see CLAUDE.md on cr.mogrify), so mixing in a psycopg2 %s
                # parameter here would risk colliding with any literal "%s"
                # substring already present in the compiled query.
                self.env.cr.execute('SELECT * FROM (%s) ks_preview_r LIMIT %d' % (query, limit))
                col_order = [d.name for d in self.env.cr.description]
                raw_rows = self.env.cr.fetchall()
        except Exception as error:  # noqa: BLE001
            return {'ok': False, 'error': str(error)}

        label_by_col = {name: label for name, label, _t in cols}
        rows = [
            {col_order[i]: self._ks_jsonify(value) for i, value in enumerate(row)}
            for row in raw_rows
        ]
        for col_name, relation in relation_by_col.items():
            if col_name not in col_order:
                continue
            ids = {r[col_name] for r in rows if r.get(col_name)}
            if not ids:
                continue
            comodel = self.env.get(relation)
            if comodel is None:
                continue
            names = dict(comodel.sudo().browse(ids).exists().mapped(
                lambda rec: (rec.id, rec.display_name)))
            for row in rows:
                if row.get(col_name) in names:
                    row[col_name] = names[row[col_name]]
        return {
            'ok': True,
            'total': total,
            'columns': [{'name': c, 'label': label_by_col.get(c, c)} for c in col_order if c != 'id'],
            'rows': rows,
        }

    @staticmethod
    def _ks_jsonify(value):
        """cr.execute bypasses the ORM, so values come back as raw psycopg2
        types (Decimal, date/datetime, NULL-as-None...) that need converting.

        None specifically: a NULL aggregate (SUM over zero matching rows) is
        a real, common case here - "sold: NULL" for a never-sold product -
        and plain ``None`` fails to marshal over XML-RPC at all
        ("cannot marshal None unless allow_none is enabled"). Odoo's own
        convention for "no value" in an RPC payload is ``False``, not
        ``null`` (see any ORM read() result), so None is normalised to that
        rather than left as a transport-format landmine.
        """
        if value is None:
            return False
        if isinstance(value, decimal.Decimal):
            return float(value)
        if isinstance(value, (datetime.date, datetime.datetime)):
            return str(value)
        return value

    @api.model
    def ks_create_report(self, payload):
        """Create a ks.report.builder from the canvas and return an action
        opening it, so the user lands on the normal form to review/deploy.

        ``payload`` = {name, model, lang_id?, columns: [{path, label,
        collapse?, flat_domain?}]}
        """
        model_name = (payload or {}).get('model')
        columns = (payload or {}).get('columns') or []
        if not model_name:
            raise UserError(_("Pick a base model first."))
        if not columns:
            raise UserError(_("Add at least one column before creating the report."))
        model_rec = self.env['ir.model'].search([('model', '=', model_name)], limit=1)
        if not model_rec:
            raise UserError(_("Model %s does not exist.", model_name))

        lines = [(0, 0, vals) for vals in self._ks_column_vals(columns)]

        report = self.env['ks.report.builder'].create({
            'name': payload.get('name') or _('Untitled Report'),
            'model_id': model_rec.id,
            'domain': payload.get('domain') or '[]',
            'lang_id': payload.get('lang_id') or False,
            'skip_company_check': bool(payload.get('skip_company_check')),
            'field_ids': lines,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': report.name,
            'res_model': 'ks.report.builder',
            'res_id': report.id,
            'view_mode': 'form',
            # `views` is NOT optional for a hand-built action dict: the web
            # client's _preprocessAction does `action.views.map(...)`
            # unconditionally (web/.../actions/action_service.js:442), so
            # omitting it throws "Cannot read properties of undefined
            # (reading 'map')" and the button silently does nothing. An
            # action READ from the database always has it, which is why this
            # only shows up for dicts returned from Python.
            'views': [[False, 'form']],
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Editing an EXISTING report from the canvas
    # ------------------------------------------------------------------

    def _ks_register_path_nodes(self, base_model, path, nodes, node_index):
        """Walk every hop of ``path`` except the last (the leaf column
        itself is not a node) and register a canvas node for each, reusing
        one already registered for the same prefix - two columns sharing
        `order_line.*` must reconstruct as ONE "Sales Order Line" card, not
        two. ``nodes``/``node_index`` are mutated in place; ``node_index``
        maps a prefix tuple to that node's position in ``nodes``.
        """
        if () not in node_index:
            node_index[()] = 0  # the base node is always nodes[0]
        segments = tuple((path or '').split('.'))[:-1]
        current_model = base_model
        prefix = ()
        for segment in segments:
            prefix = prefix + (segment,)
            if prefix not in node_index:
                field = self.env[current_model]._fields.get(segment)
                if field is None or not field.relational:
                    return  # a stale/invalid path - _ks_build_query will
                    # raise its own clear error when the report is next used;
                    # silently skipping here just means this node/column
                    # doesn't reappear on the canvas.
                parent_index = node_index[prefix[:-1]]
                via_list = nodes[parent_index]['via_x2many'] or field.type in KS_X2MANY_TYPES
                node_index[prefix] = len(nodes)
                nodes.append({
                    'model': field.comodel_name,
                    'parent_index': parent_index,
                    'rel_field': segment,
                    'via_x2many': via_list,
                })
            current_model = nodes[node_index[prefix]]['model']

    @api.model
    def ks_load_report(self, report_id):
        """Reconstruct a canvas (nodes + columns) for an EXISTING report, so
        "Edit in Designer" doesn't just start from a blank canvas.

        The node graph is rebuilt from the columns' own dotted paths every
        time (not stored/replayed from a previous session) - simpler and
        immune to drift if a column was since added or removed on the
        ordinary form, at the cost of not remembering exactly how the cards
        were arranged. Positions are then assigned by the client's own
        auto-layout, the same cascading placement used when expanding a
        relation by hand.
        """
        report = self.env['ks.report.builder'].browse(report_id)
        if not report.exists():
            raise UserError(_("Report not found."))
        if any(line.kind == 'expression' for line in report.field_ids):
            # A Computed column has no canvas representation at all (it is
            # arithmetic over other columns' aliases, not a field path), and
            # ks_update_report replaces the WHOLE column set - loading this
            # report would silently drop that column the moment it is saved
            # from here. Refusing to open is the honest choice; the ordinary
            # form still edits it fine.
            raise UserError(_(
                "\"%s\" has a Computed column, which the visual designer "
                "cannot represent. Edit it from the report form instead - "
                "opening it here would delete that column on save.",
                report.name))

        base_model = report.model_id.model
        nodes = [{'model': base_model, 'parent_index': None, 'rel_field': None, 'via_x2many': False}]
        node_index = {(): 0}
        columns = []
        for line in report.field_ids:
            if line.kind == 'aggregate':
                if line.agg_base_path:
                    # _ks_register_path_nodes registers every hop EXCEPT the
                    # last (a normal path's last hop is a scalar leaf, not a
                    # node). For agg_base_path the last hop IS the dimension
                    # itself (e.g. "Product" - a many2one) and must become a
                    # node, since that is the card the Σ button appears on -
                    # the trailing ".x" is a throwaway segment that makes the
                    # real path's full length get registered.
                    self._ks_register_path_nodes(base_model, line.agg_base_path + '.x', nodes, node_index)
                columns.append({
                    'kind': 'aggregate',
                    'label': line.label,
                    'path': '%s → %s' % (
                        line.agg_model_id.model, line.agg_base_path or _('this record')),
                    'aggFunction': line.agg_function,
                    'agg_base_path': line.agg_base_path or '',
                    'agg_model_id': line.agg_model_id.id,
                    'agg_link_field_id': line.agg_link_field_id.id,
                    'agg_measure_field_id': line.agg_measure_field_id.id,
                    'agg_function': line.agg_function,
                    'agg_domain': line.agg_domain or False,
                })
                continue
            self._ks_register_path_nodes(base_model, line.path, nodes, node_index)
            try:
                info = ks_split_path(self.env, base_model, line.path)
                crosses_list = info['x2many'] is not None
                list_model = info['x2many'].comodel_name if crosses_list else False
            except Exception:  # noqa: BLE001 - a stale path; show it inert rather than fail the whole load
                crosses_list, list_model = bool(line.collapse), False
            columns.append({
                'kind': 'path',
                'label': line.label,
                'path': line.path,
                'collapse': line.collapse or False,
                'flat_domain': line.flat_domain or False,
                'crossesList': crosses_list,
                'listModel': list_model,
            })

        return {
            'report_id': report.id,
            'name': report.name,
            'model': {'model': base_model, 'name': report.model_id.name},
            'lang_id': report.lang_id.id,
            'domain': report.domain or '[]',
            'nodes': nodes,
            'columns': columns,
        }

    @api.model
    def ks_update_report(self, report_id, payload):
        """Save canvas edits back onto an EXISTING report.

        Replaces the WHOLE column set (``(5, 0, 0)`` then recreate) rather
        than diffing - the canvas is the source of truth for "what the
        columns are" once editing here, the same way saving the ordinary
        Columns list replaces field_ids wholesale on write. The base model
        is deliberately not changed from here (payload's model is ignored) -
        changing it would invalidate every existing column's path, which is
        exactly the kind of structural edit the form already requires an
        Undeploy for; the designer doesn't attempt it either.
        """
        report = self.env['ks.report.builder'].browse(report_id)
        if not report.exists():
            raise UserError(_("Report not found."))
        if report.state == 'deployed':
            raise UserError(_(
                "Undeploy \"%s\" before editing its columns - the same rule "
                "applies here as on the report form.", report.name))
        columns = (payload or {}).get('columns') or []
        if not columns:
            raise UserError(_("Add at least one column before saving."))

        lines = [Command.clear()] + [(0, 0, vals) for vals in self._ks_column_vals(columns)]
        report.write({
            'name': payload.get('name') or report.name,
            'domain': payload.get('domain') or '[]',
            'lang_id': payload.get('lang_id') or False,
            'field_ids': lines,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': report.name,
            'res_model': 'ks.report.builder',
            'res_id': report.id,
            'view_mode': 'form',
            'views': [[False, 'form']],
            'target': 'current',
        }
