/** @odoo-module **/

import { Component, onWillStart, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { _t } from "@web/core/l10n/translation";
import { DomainSelector } from "@web/core/domain_selector/domain_selector";

/**
 * Visual, ER-diagram style report designer.
 *
 * An alternative authoring UI only: it produces an ordinary ks.report.builder
 * record (see ks_report_designer.py), so the query engine, validation and
 * deploy pipeline are the already-tested ones.
 *
 * Interaction model:
 *  - the canvas holds model "nodes"; expanding a relation spawns the related
 *    model as a new node joined by a connector. A node can only be created by
 *    following a real relation, so every path is valid by construction.
 *  - fields are dragged from a node onto the Columns panel (copy), and columns
 *    are dragged within the panel to reorder (move) - both native HTML5 drag
 *    and drop with a discriminating payload, not two drag systems.
 */

let nodeSeq = 1;

export class KsReportDesigner extends Component {
    static template = "ks_report_builder.KsReportDesigner";
    static props = { ...standardActionServiceProps };
    static components = { DomainSelector };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.canvasRef = useRef("canvas");

        this.state = useState({
            reportName: "",
            baseModel: null,
            modelQuery: "",
            modelResults: [],
            nodes: [],
            columns: [],
            busy: false,
            showTechnical: false,
            agg: null,
            reportDomain: "[]",
            domainEditor: null,
            preview: null,
            previewDialog: null,
            editingReportId: null,
            loadingReport: false,
            zoom: 1,
        });

        // Transient drag bookkeeping. Not in state: it must not re-render.
        this.drag = { kind: null, payload: null };
        this.nodeDrag = null;

        onWillStart(async () => {
            this.languages = await this.orm.searchRead(
                "res.lang", [["active", "=", true]], ["id", "name", "code"]
            );
            // "Edit in Designer" opens this action with report_id in its
            // params, which replays that report's columns onto the canvas.
            const reportId = this.props.action?.params?.report_id;
            if (reportId) {
                await this.loadReportIntoDesigner(reportId);
            }
        });

        // Debounced: every drag/reorder/collapse-change would otherwise fire
        // its own round trip.
        this.refreshPreview = useDebounced(this._refreshPreview, 400);
    }

    // ------------------------------------------------------- edit existing

    /** Rebuild the canvas from an existing report's columns; the server
     *  reconstructs the node graph from their dotted paths (ks_load_report).
     *  Positions use the same cascading layout as expandRelation(). */
    async loadReportIntoDesigner(reportId) {
        this.state.loadingReport = true;
        try {
            const data = await this.orm.call("ks.report.designer", "ks_load_report", [reportId]);
            this.state.editingReportId = data.report_id;
            this.state.reportName = data.name;
            this.state.baseModel = { model: data.model.model, name: data.model.name };
            this.state.reportDomain = data.domain || "[]";
            this.state.nodes = [];
            this.state.columns = [];

            const clientIdByServerIndex = {};
            for (let i = 0; i < data.nodes.length; i++) {
                const n = data.nodes[i];
                const parentClientId = n.parent_index === null
                    ? null : clientIdByServerIndex[n.parent_index];
                const parentNode = parentClientId === null
                    ? null : this.state.nodes.find((node) => node.id === parentClientId);
                const siblings = parentNode
                    ? this.state.nodes.filter((node) => node.parentId === parentNode.id).length
                    : 0;
                const x = parentNode ? parentNode.x + 360 : 40;
                const y = parentNode ? parentNode.y + siblings * 220 : 40;
                await this.addNode(n.model, n.model, parentClientId, n.rel_field, n.via_x2many, x, y);
                clientIdByServerIndex[i] = this.state.nodes[this.state.nodes.length - 1].id;
            }
            // Columns arrive shaped exactly like the ones addColumn() and
            // addAggregateColumn() build client-side.
            this.state.columns = data.columns;
            this.refreshPreview();
        } finally {
            this.state.loadingReport = false;
        }
    }

    // ------------------------------------------------------------- preview

    /** The one place ``state.columns`` becomes what the server expects, for
     *  preview, create and update alike - so a preview can never describe a
     *  different report from the one Create Report builds. */
    _columnsPayload() {
        return this.state.columns.map((c) => (
            c.kind === "aggregate"
                ? {
                    kind: "aggregate",
                    label: c.label,
                    agg_base_path: c.agg_base_path,
                    agg_model_id: c.agg_model_id,
                    agg_link_field_id: c.agg_link_field_id,
                    agg_measure_field_id: c.agg_measure_field_id,
                    agg_function: c.agg_function,
                    agg_domain: c.agg_domain || false,
                }
                : {
                    path: c.path,
                    label: c.label,
                    collapse: c.collapse || false,
                    flat_domain: c.flat_domain || false,
                }
        ));
    }

    /** Shared by the sidebar strip and the expanded dialog, so both preview
     *  the same columns - only the row limit differs. */
    _previewPayload() {
        return { model: this.state.baseModel.model, columns: this._columnsPayload() };
    }

    /** Real data from the real query builder, with zero writes (see
     *  ks_preview). Runs after every change that could affect the result. */
    async _refreshPreview() {
        if (!this.state.baseModel || !this.state.columns.length) {
            this.state.preview = null;
            return;
        }
        const requestId = (this._previewRequestId = (this._previewRequestId || 0) + 1);
        this.state.preview = { loading: true };
        const result = await this.orm.call(
            "ks.report.designer", "ks_preview", [this._previewPayload()]
        );
        // A column may have changed while this request was in flight; a
        // stale response must not overwrite a newer one.
        if (requestId !== this._previewRequestId) {
            return;
        }
        this.state.preview = { loading: false, ...result };
        // Keep an already-open dialog in sync with the sidebar strip.
        if (this.state.previewDialog) {
            this.refreshPreviewDialog();
        }
    }

    /** Manual refresh: bypasses the debounce and cancels any pending
     *  debounced call, so it cannot fire a redundant second request. */
    forceRefreshPreview() {
        this.refreshPreview.cancel();
        this._refreshPreview();
    }

    /** The same preview in a bigger dialog, up to 200 rows instead of the
     *  sidebar strip's 8. */
    async openPreviewDialog() {
        this.state.previewDialog = { loading: true };
        await this.refreshPreviewDialog();
    }

    async refreshPreviewDialog() {
        if (!this.state.baseModel || !this.state.columns.length) {
            return;
        }
        const requestId = (this._previewDialogRequestId = (this._previewDialogRequestId || 0) + 1);
        this.state.previewDialog = { loading: true };
        const result = await this.orm.call(
            "ks.report.designer", "ks_preview", [this._previewPayload(), 200]
        );
        if (requestId !== this._previewDialogRequestId) {
            return;
        }
        this.state.previewDialog = { loading: false, ...result };
    }

    closePreviewDialog() {
        this.state.previewDialog = null;
    }

    /** Clicking the dark backdrop closes the dialog, but not a click that
     *  started inside the box and bubbled up.
     *  A real method, never an inline `t-on-click="(ev) => { if (...) }"`:
     *  OWL's template compiler takes single expressions, not control-flow
     *  bodies, and fails to compile the WHOLE template if given one. */
    onPreviewBackdropClick(ev) {
        if (ev.target === ev.currentTarget) {
            this.closePreviewDialog();
        }
    }

    // ---------------------------------------------------------------- model

    async searchModels() {
        const query = this.state.modelQuery.trim();
        if (query.length < 2) {
            this.state.modelResults = [];
            return;
        }
        // Ranked and tokenised server side; see ks_search_models.
        this.state.modelResults = await this.orm.call(
            "ks.report.designer", "ks_search_models", [query, 15]
        );
    }

    /** Clear the base-model search box and its dropdown in one click. */
    clearModelQuery() {
        this.state.modelQuery = "";
        this.state.modelResults = [];
    }

    async pickBaseModel(rec) {
        this.state.baseModel = { model: rec.model, name: rec.name };
        this.state.modelResults = [];
        this.state.modelQuery = "";
        this.state.nodes = [];
        this.state.columns = [];
        this.state.preview = null;
        this.state.previewDialog = null;
        if (!this.state.reportName) {
            this.state.reportName = _t("%s report", rec.name);
        }
        await this.addNode(rec.model, rec.name, null, null, false, 40, 40);
    }

    /** Fetch a model's schema and place it on the canvas. */
    async addNode(model, name, parentId, relField, viaX2many, x, y) {
        const schema = await this.orm.call(
            "ks.report.designer", "ks_model_schema",
            [model, this.state.showTechnical]
        );
        const parent = this.state.nodes.find((n) => n.id === parentId);
        // The dotted prefix this node's fields hang off, built from the chain
        // of relations actually followed.
        const prefix = parent
            ? `${parent.prefix}${relField}.`
            : "";
        this.state.nodes.push({
            id: nodeSeq++,
            model,
            name: schema.name || name,
            columns: schema.columns,
            relations: schema.relations,
            parentId,
            relField,
            relLabel: relField ? relField : null,
            viaX2many,
            prefix,
            x,
            y,
            collapsed: false,
            filter: '',
        });
    }

    async expandRelation(node, rel) {
        const already = this.state.nodes.find(
            (n) => n.parentId === node.id && n.relField === rel.name
        );
        if (already) {
            this.notification.add(_t("%s is already on the canvas.", rel.string), {
                type: "info",
            });
            return;
        }
        const siblings = this.state.nodes.filter((n) => n.parentId === node.id).length;
        await this.addNode(
            rel.relation, rel.string, node.id, rel.name,
            rel.kind === "x2many" || node.viaX2many,
            node.x + 360,
            node.y + siblings * 220
        );
    }

    /** A model can have 90+ fields, so each node carries its own quick
     *  filter over both of its lists. */
    matching(node, entries) {
        const q = (node.filter || "").trim().toLowerCase();
        if (!q) {
            return entries;
        }
        return entries.filter(
            (e) => e.string.toLowerCase().includes(q)
                || e.name.toLowerCase().includes(q)
                // relations are also searchable by what they point at
                || (e.relation || "").toLowerCase().includes(q)
        );
    }

    setNodeFilter(node, ev) {
        node.filter = ev.target.value;
    }

    /** Clear one card's field filter. */
    clearNodeFilter(node) {
        node.filter = "";
    }

    /** Removing a node removes its descendants and any columns they produced. */
    removeNode(node) {
        const doomed = new Set([node.id]);
        let grew = true;
        while (grew) {
            grew = false;
            for (const n of this.state.nodes) {
                if (n.parentId !== null && doomed.has(n.parentId) && !doomed.has(n.id)) {
                    doomed.add(n.id);
                    grew = true;
                }
            }
        }
        const prefixes = this.state.nodes
            .filter((n) => doomed.has(n.id))
            .map((n) => n.prefix)
            .filter((p) => p);
        this.state.nodes = this.state.nodes.filter((n) => !doomed.has(n.id));
        this.state.columns = this.state.columns.filter((c) => {
            if (c.kind === "aggregate") {
                // an aggregate correlates on a card's path, which no longer
                // resolves once that card is gone
                return !prefixes.some((p) => `${c.agg_base_path}.` === p);
            }
            return !prefixes.some((p) => c.path.startsWith(p));
        });
        if (this.state.agg && !this.state.nodes.some((n) => n.id === this.state.agg.node.id)) {
            this.state.agg = null;
        }
    }

    // ------------------------------------------------------------ connectors

    /** Curved connectors between each node and its parent, drawn under the
     *  cards by an SVG layer. An S-shaped cubic bezier reads as a flow even
     *  when a node is dragged off to the side, where a straight line would
     *  cut through other cards. */
    get connectors() {
        const byId = Object.fromEntries(this.state.nodes.map((n) => [n.id, n]));
        return this.state.nodes
            .filter((n) => n.parentId && byId[n.parentId])
            .map((n) => {
                const p = byId[n.parentId];
                const x1 = p.x + 292, y1 = p.y + 28;
                const x2 = n.x, y2 = n.y + 28;
                const pull = Math.max(40, Math.abs(x2 - x1) / 2);
                return {
                    id: n.id,
                    path: `M ${x1} ${y1} C ${x1 + pull} ${y1}, ${x2 - pull} ${y2}, ${x2} ${y2}`,
                    labelX: (x1 + x2) / 2,
                    labelY: (y1 + y2) / 2 - 8,
                    label: n.relLabel,
                    dashed: n.viaX2many,
                };
            });
    }

    // ----------------------------------------------------------------- zoom

    /** 50%-150% in 10% steps, so a canvas with many expanded cards can be
     *  seen as a whole. */
    zoomIn() {
        this.state.zoom = Math.min(1.5, Math.round((this.state.zoom + 0.1) * 10) / 10);
    }

    zoomOut() {
        this.state.zoom = Math.max(0.5, Math.round((this.state.zoom - 0.1) * 10) / 10);
    }

    resetZoom() {
        this.state.zoom = 1;
    }

    // ------------------------------------------------------- node dragging

    onNodePointerDown(node, ev) {
        if (ev.target.closest(".ks-no-drag")) {
            return;
        }
        ev.preventDefault();
        // canvasRef is the ZOOMED content wrapper, so its bounding box is
        // already post-transform. Dividing by the current zoom converts a
        // screen-pixel delta back to the unscaled space node.x/y live in, so
        // dragging tracks the cursor 1:1 at any zoom level.
        const rect = this.canvasRef.el.getBoundingClientRect();
        const zoom = this.state.zoom;
        this.nodeDrag = {
            node,
            dx: (ev.clientX - rect.left) / zoom - node.x,
            dy: (ev.clientY - rect.top) / zoom - node.y,
        };
        const move = (e) => {
            if (!this.nodeDrag) {
                return;
            }
            const r = this.canvasRef.el.getBoundingClientRect();
            const z = this.state.zoom;
            this.nodeDrag.node.x = Math.max(0, (e.clientX - r.left) / z - this.nodeDrag.dx);
            this.nodeDrag.node.y = Math.max(0, (e.clientY - r.top) / z - this.nodeDrag.dy);
        };
        const up = () => {
            this.nodeDrag = null;
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
    }

    // ------------------------------------------------------ column drag/drop

    onFieldDragStart(node, field, ev) {
        this.drag = {
            kind: "field",
            payload: { path: `${node.prefix}${field.name}`, field, node },
        };
        ev.dataTransfer.effectAllowed = "copy";
        ev.dataTransfer.setData("text/plain", `${node.prefix}${field.name}`);
    }

    onColumnDragStart(index, ev) {
        this.drag = { kind: "column", payload: { index } };
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", String(index));
    }

    onPanelDragOver(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = this.drag.kind === "column" ? "move" : "copy";
    }

    async onPanelDrop(ev, targetIndex = null) {
        ev.preventDefault();
        ev.stopPropagation();
        const drag = this.drag;
        this.drag = { kind: null, payload: null };
        if (!drag.kind) {
            return;
        }
        if (drag.kind === "column") {
            const from = drag.payload.index;
            let to = targetIndex === null ? this.state.columns.length - 1 : targetIndex;
            if (from === to) {
                return;
            }
            const cols = [...this.state.columns];
            const [moved] = cols.splice(from, 1);
            if (from < to) {
                to -= 1;
            }
            cols.splice(to + (targetIndex === null ? 1 : 0), 0, moved);
            this.state.columns = cols;
            return; // pure reorder - the result set is unaffected
        }
        await this.addColumn(drag.payload, targetIndex);
        this.refreshPreview();
    }

    /** Ask the server what this path means before accepting it, so a bad
     *  drop is refused immediately rather than at save or deploy time. */
    async addColumn(payload, targetIndex = null) {
        const { path, field } = payload;
        const info = await this.orm.call(
            "ks.report.designer", "ks_preview_path",
            [this.state.baseModel.model, path]
        );
        if (!info.ok) {
            this.notification.add(info.error, { type: "danger", title: _t("Cannot use this field") });
            return;
        }
        // A path through a list must say how many rows become one value.
        // Count is the only thing meaningful when the path ends ON the
        // list; otherwise listing the values is the least surprising.
        const collapse = info.crosses_list
            ? (info.terminal_is_list ? "count" : "list")
            : false;
        // Runs after the collapse is known, and compares it: path alone is
        // too strict (a First/Last pair off one path is legitimate), while
        // exempting every list-crossing column is too loose and lets the same
        // chained field be added without limit.
        // A fresh column never carries a Filter, so an existing one that does
        // is a different column and must not block this drop.
        if (this.state.columns.some(
            (c) => c.path === path && (c.collapse || false) === collapse && !c.flat_domain
        )) {
            this.notification.add(_t("%s is already a column.", path), { type: "info" });
            return;
        }
        const column = {
            path,
            label: field.string,
            ttype: field.ttype,
            crossesList: info.crosses_list,
            listModel: info.list_model,
            collapse,
        };
        const cols = [...this.state.columns];
        cols.splice(targetIndex === null ? cols.length : targetIndex, 0, column);
        this.state.columns = cols;
    }

    /** Is this field already one of the report's columns? Drives the tick
     *  box. Matches on path alone: the box answers "is this field included?",
     *  so a First/Last pair off one path still shows as ticked once. */
    isColumnPath(path) {
        return this.state.columns.some((c) => c.path === path);
    }

    /** Tick/untick a field from its card - the keyboard- and click-friendly
     *  twin of dragging it into the Columns panel. Routed through the same
     *  addColumn() the drop handler uses, so a ticked column and a dragged
     *  one cannot end up configured differently. */
    async toggleColumn(node, field) {
        const path = node.prefix + field.name;
        if (this.isColumnPath(path)) {
            // Untick removes every column on this path, including a
            // First/Last pair, so the box cannot stay ticked with nothing
            // left to untick.
            this.state.columns = this.state.columns.filter((c) => c.path !== path);
            this.refreshPreview();
            return;
        }
        await this.addColumn({ path, field });
        this.refreshPreview();
    }

    /** Space/Enter toggles a focused field row, so the Columns list can be
     *  built without a mouse. A real method, never an inline arrow with an
     *  `if` in it - that breaks OWL's template compiler outright. */
    onFieldKeydown(node, field, ev) {
        if (ev.key !== "Enter" && ev.key !== " ") {
            return;
        }
        ev.preventDefault();   // stop Space scrolling the canvas
        this.toggleColumn(node, field);
    }

    setColumnLabel(col, ev) {
        col.label = ev.target.value;
        // the label changes only the heading, not the data
    }

    setColumnCollapse(col, ev) {
        col.collapse = ev.target.value;
        this.refreshPreview();
    }

    removeColumn(index) {
        this.state.columns = this.state.columns.filter((_c, i) => i !== index);
        this.refreshPreview();
    }

    /** ks_preview runs raw SQL, not the ORM's read(), so the preview trades
     *  "looks exactly like the deployed report" for zero writes and real
     *  numbers. "No value" arrives as `false` (see _ks_jsonify), not `null`.
     *  Only that family gets a placeholder - a falsy number like 0 must still
     *  render as 0. */
    formatCell(value) {
        return value === null || value === undefined || value === "" || value === false
            ? "—" : value;
    }

    // --------------------------------------------------------------- domains

    /** One editor serves all three filters - the report's own Filter, a list
     *  column's flat_domain and an aggregate's agg_domain - which differ only
     *  by target model and destination, so `kind` dispatches on apply. */
    openReportDomain() {
        if (!this.state.baseModel) {
            return;
        }
        this.state.domainEditor = {
            kind: "report",
            title: _t("Filter which %s rows appear", this.state.baseModel.name),
            resModel: this.state.baseModel.model,
            domain: this.state.reportDomain || "[]",
        };
    }

    openColumnDomain(index) {
        const col = this.state.columns[index];
        if (!col || !col.listModel) {
            return;
        }
        this.state.domainEditor = {
            kind: "column",
            index,
            title: _t("Filter the rows counted in %s", col.label),
            resModel: col.listModel,
            domain: col.flat_domain || "[]",
        };
    }

    openAggDomain() {
        const agg = this.state.agg;
        if (!agg || !agg.source) {
            return;
        }
        this.state.domainEditor = {
            kind: "agg",
            title: _t("Filter the %s rows aggregated", agg.source.name),
            resModel: agg.source.model,
            domain: agg.domain || "[]",
        };
    }

    onDomainChange(domain) {
        if (this.state.domainEditor) {
            this.state.domainEditor.domain = domain;
        }
    }

    applyDomain() {
        const ed = this.state.domainEditor;
        if (!ed) {
            return;
        }
        const value = ed.domain === "[]" ? false : ed.domain;
        if (ed.kind === "report") {
            this.state.reportDomain = ed.domain || "[]";
        } else if (ed.kind === "column") {
            this.state.columns[ed.index].flat_domain = value;
            this.refreshPreview(); // narrows which child rows count - changes the result
        } else if (ed.kind === "agg") {
            this.state.agg.domain = value;
        }
        this.state.domainEditor = null;
    }

    closeDomain() {
        this.state.domainEditor = null;
    }

    // ------------------------------------------------------------ aggregate

    /** An aggregate correlates another table back to a dimension: either a
     *  card reached by many2one hops, or the base card itself (empty
     *  agg_base_path), which is what makes one-row-per-X reports possible.
     *  Cards reached through a list are excluded - their rows are not a
     *  single value to correlate on. */
    canAggregate(node) {
        return !node.viaX2many;
    }

    async openAggregate(node) {
        this.state.agg = {
            node,
            basePath: node.prefix.replace(/\.$/, ""),
            dimension: node.model,
            query: "",
            sources: [],
            source: null,
            fields: null,
            link: null,
            measure: null,
            fn: "sum",
            label: "",
            loading: false,
        };
        await this.searchAggregateSources();
    }

    closeAggregate() {
        this.state.agg = null;
    }

    async searchAggregateSources() {
        const agg = this.state.agg;
        if (!agg) {
            return;
        }
        agg.loading = true;
        try {
            agg.sources = await this.orm.call(
                "ks.report.designer", "ks_aggregate_sources",
                [agg.dimension, agg.query, 25]
            );
        } finally {
            agg.loading = false;
        }
    }

    async pickAggregateSource(rec) {
        const agg = this.state.agg;
        agg.source = rec;
        agg.fields = await this.orm.call(
            "ks.report.designer", "ks_aggregate_fields", [rec.model, agg.dimension]
        );
        // one link is the common case, and has only one right answer
        agg.link = agg.fields.links.length ? agg.fields.links[0].id : null;
        agg.measure = agg.fields.measures.length ? agg.fields.measures[0].id : null;
        this.syncAggregateLabel();
    }

    syncAggregateLabel() {
        const agg = this.state.agg;
        if (!agg || !agg.source) {
            return;
        }
        const measure = (agg.fields?.measures || []).find((f) => f.id === agg.measure);
        const fn = agg.fn === "count" ? _t("Count of") : agg.fn.toUpperCase();
        agg.label = agg.fn === "count"
            ? _t("Count of %s", agg.source.name)
            : `${fn} ${measure ? measure.field_description : agg.source.name}`;
    }

    setAgg(key, ev) {
        const agg = this.state.agg;
        const raw = ev.target.value;
        agg[key] = ["link", "measure"].includes(key) ? Number(raw) : raw;
        if (key !== "label") {
            this.syncAggregateLabel();
        }
    }

    get aggregateReady() {
        const a = this.state.agg;
        return Boolean(a && a.source && a.link && (a.fn === "count" || a.measure));
    }

    addAggregateColumn() {
        const a = this.state.agg;
        if (!this.aggregateReady) {
            return;
        }
        // As in addColumn's guard: an aggregate is defined entirely by these
        // six settings, so two with identical ones are the same column twice.
        // The server's own duplicate check ignores the label too.
        if (this.state.columns.some(
            (c) => c.kind === "aggregate"
                && c.agg_base_path === a.basePath
                && c.agg_model_id === a.source.id
                && c.agg_link_field_id === a.link
                && c.agg_measure_field_id === a.measure
                && c.agg_function === a.fn
                && (c.agg_domain || false) === (a.domain || false)
        )) {
            this.notification.add(
                _t("That aggregate is already a column."), { type: "info" });
            return;
        }
        this.state.columns = [...this.state.columns, {
            kind: "aggregate",
            label: a.label || a.source.name,
            path: `${a.source.model} → ${a.basePath}`,
            aggFunction: a.fn,
            agg_base_path: a.basePath,
            agg_model_id: a.source.id,
            agg_link_field_id: a.link,
            // COUNT(*) still needs a measure field on the line to satisfy
            // the model's completeness constraint
            agg_measure_field_id: a.measure,
            agg_function: a.fn,
            agg_domain: a.domain || false,
        }];
        this.state.agg = null;
        this.refreshPreview();
    }

    // ------------------------------------------------------------- creation

    /** Two or more "List as text" columns off the SAME list model do not
     *  line up row-for-row: each is its own independent sorted list, so
     *  "Product: A, B" beside "Quantity: 5, 10, 15" cannot be read as pairs
     *  even though the instinct is to match them positionally. Warned about
     *  rather than silently allowed - the fix is First/Last, or making that
     *  list the report's own base model. */
    get misalignedListModels() {
        const counts = {};
        for (const c of this.state.columns) {
            // Both list modes are affected: the risk comes from two
            // independent lists collapsed side by side, not from
            // de-duplication.
            if ((c.collapse === "list" || c.collapse === "list_all") && c.listModel) {
                counts[c.listModel] = (counts[c.listModel] || 0) + 1;
            }
        }
        return Object.keys(counts).filter((model) => counts[model] > 1);
    }

    get canCreate() {
        return Boolean(this.state.baseModel) && this.state.columns.length > 0 && !this.state.busy;
    }

    /** Back to the report's own form, deliberately WITHOUT saving first - an
     *  escape hatch, not a shortcut for Save Changes. Built client-side: the
     *  id is already known in edit mode. */
    async goToReportForm() {
        if (!this.state.editingReportId) {
            return;
        }
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ks.report.builder",
            res_id: this.state.editingReportId,
            view_mode: "form",
            views: [[false, "form"]],
            target: "current",
        });
    }

    async createReport() {
        if (!this.canCreate) {
            return;
        }
        this.state.busy = true;
        try {
            const payload = {
                name: this.state.reportName || undefined,
                model: this.state.baseModel.model,
                domain: this.state.reportDomain || "[]",
                skip_company_check: true,
                columns: this._columnsPayload(),
            };
            // Editing an existing report calls ks_update_report (replaces
            // its columns wholesale) instead of ks_create_report; only the
            // destination differs.
            const action = this.state.editingReportId
                ? await this.orm.call(
                    "ks.report.designer", "ks_update_report",
                    [this.state.editingReportId, payload])
                : await this.orm.call("ks.report.designer", "ks_create_report", [payload]);
            await this.action.doAction(action);
        } finally {
            this.state.busy = false;
        }
    }
}

registry.category("actions").add("ks_report_designer", KsReportDesigner);
