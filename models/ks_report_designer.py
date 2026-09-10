"""Backend for the visual (ER-diagram style) report designer.

A thin layer: the designer is an alternative authoring UI, not a second
reporting engine. It produces ordinary ``ks.report.builder`` records, so the
query builder, validation and deploy pipeline apply unchanged.
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

        A plain ``name ilike <query>`` ordered by name buries the obvious
        answer and misses non-contiguous matches ("sale order" vs "Sales
        Order"). The query is tokenised - every word must match - then ranked
        exact/prefix first, shorter model names before their derived cousins.
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
            if getattr(field, 'company_dependent', False):
                # Stored as jsonb keyed by company id, so the raw column
                # cannot be selected or joined directly. The canvas has no
                # per-report company setting, so these are excluded here.
                # See KS_COMPANY_DEPENDENT_ERROR.
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

        Only tables with a stored many2one to D and at least one stored
        numeric field are offered, so the canvas cannot express a combination
        `_check_aggregate` would reject at save time.
        """
        links = self.env['ir.model.fields'].search_read(
            [('ttype', '=', 'many2one'), ('relation', '=', dimension_model),
             ('store', '=', True), ('company_dependent', '=', False)],
            ['model'], limit=2000)
        candidates = sorted({link['model'] for link in links})
        if not candidates:
            return []
        # COUNT is meaningful without a numeric column, but every other
        # aggregator needs one, so a table with no measure is nearly useless.
        measures = self.env['ir.model.fields'].search_read(
            [('model', 'in', candidates),
             ('ttype', 'in', list(KS_AGGREGATE_NUMERIC_TYPES)),
             ('store', '=', True), ('company_dependent', '=', False)],
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
            # dots and spaces are interchangeable to a typing user
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
             ('relation', '=', dimension_model), ('store', '=', True),
             ('company_dependent', '=', False)],
            ['id', 'name', 'field_description'])
        measures = fields_model.search_read(
            # 'id' is numeric and stored, but aggregating a primary key is
            # meaningless - and it sorts first, so it became the default.
            [('model', '=', model_name),
             ('ttype', 'in', list(KS_AGGREGATE_NUMERIC_TYPES)),
             ('store', '=', True), ('name', '!=', 'id'),
             ('company_dependent', '=', False)],
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
        crosses_list = bool(info['hops'])
        return {
            'ok': True,
            'crosses_list': crosses_list,
            # The model the collapse and the List Filter actually apply to is
            # the far end of the chain, which for a multi-list path is not the
            # first list crossed.
            'list_model': info['child_model'] if crosses_list else False,
            'list_count': len(info['hops']),
            'terminal_is_list': crosses_list and info['field'] is None,
            'needs_collapse': crosses_list and not collapse,
        }

    def _ks_column_vals(self, columns):
        """Canvas column dicts to ks.report.builder.field vals.

        Shared by ks_create_report and ks_preview, so a preview can never
        show something different from what Create Report persists.
        """
        lines = []
        for index, col in enumerate(columns):
            kind = col.get('kind') or 'path'
            vals = {
                'kind': kind,
                'sequence': (index + 1) * 10,
                'label': col.get('label') or col.get('path') or _('Column'),
            }
            if kind == 'aggregate':
                # Ids come from ks_aggregate_fields and go through the same
                # _check_aggregate constraint as the form's own columns.
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

        ``limit`` defaults to 8 for the sidebar strip and is clamped
        server-side, since this runs before the report and its access rules
        exist.

        Built via ``env.new()``: an in-memory ks.report.builder whose computed
        fields evaluate as they would on a real record, so it feeds the real
        ``_ks_build_query()``. ``@api.constrains`` do not fire on a new()
        record, but every check that matters for building SQL is raised by
        _ks_build_query's own resolvers.
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

        # The report-level Filter/Result Filter are not applied: both are set
        # on the real record after creation, so this shows every row the query
        # itself produces. Raw SQL also returns a many2one as a bare id, which
        # is resolved to a display name below.
        relation_by_col = {
            line.column_name: line.relation
            for line in report.field_ids if line.relation
        }
        cols = [(line.column_name, line.label, line.ttype) for line in report.field_ids]
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT COUNT(*) FROM (%s) ks_preview_c' % query)
                total = self.env.cr.fetchone()[0]
                # `limit` is a clamped int, not user text. The compiled query
                # is one fully-literal SQL string, so introducing a psycopg2
                # %s here could collide with a literal "%s" already in it.
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
        ttype_by_col = {name: ttype for name, _label, ttype in cols}
        return {
            'ok': True,
            'total': total,
            # ttype rides along so the dialog can right-align numeric columns
            # instead of every column looking like plain left-aligned text.
            'columns': [
                {'name': c, 'label': label_by_col.get(c, c), 'ttype': ttype_by_col.get(c)}
                for c in col_order if c != 'id'
            ],
            'rows': rows,
        }

    @staticmethod
    def _ks_jsonify(value):
        """Convert raw psycopg2 values (Decimal, date/datetime, None) for RPC.

        None is normalised to False: a NULL aggregate (SUM over zero matching
        rows) is routine here, and None cannot marshal over XML-RPC at all.
        False is Odoo's own convention for "no value" in an RPC payload.
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
            # `views` is not optional for a hand-built action dict: the web
            # client's _preprocessAction calls `action.views.map(...)`
            # unconditionally, so omitting it makes the button do nothing.
            'views': [[False, 'form']],
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Editing an EXISTING report from the canvas
    # ------------------------------------------------------------------

    def _ks_register_path_nodes(self, base_model, path, nodes, node_index):
        """Register a canvas node for every hop of ``path`` except the leaf,
        reusing one already registered for the same prefix so columns sharing
        `order_line.*` reconstruct as one card. ``nodes``/``node_index`` are
        mutated in place.
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

        The node graph is rebuilt from the columns' dotted paths every time
        rather than stored, so it cannot drift from a column changed on the
        ordinary form. Positions come from the client's own auto-layout.
        """
        report = self.env['ks.report.builder'].browse(report_id)
        if not report.exists():
            raise UserError(_("Report not found."))
        if any(line.kind == 'expression' for line in report.field_ids):
            # A Computed column has no canvas representation, and
            # ks_update_report replaces the whole column set - loading the
            # report would silently drop it on save. The ordinary form still
            # edits it.
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
                    # _ks_register_path_nodes skips the last hop, but for
                    # agg_base_path that hop IS the dimension and must become
                    # a node - it is the card the Σ button appears on. The
                    # trailing ".x" is a throwaway segment to that end.
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
                crosses_list = bool(info['hops'])
                list_model = info['child_model'] if crosses_list else False
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

        Replaces the whole column set (``(5, 0, 0)`` then recreate) rather
        than diffing: the canvas is the source of truth once editing here.
        The base model is not changed from here (payload's model is ignored),
        since that would invalidate every existing column's path.
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
