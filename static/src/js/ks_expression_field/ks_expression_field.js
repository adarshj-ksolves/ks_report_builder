/** @odoo-module **/

import { registry } from "@web/core/registry";
import { charField, CharField } from "@web/views/fields/char/char_field";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";

// Kept in sync with KS_NUMERIC_TYPES in models/ks_report_builder.py - only
// these ttypes are usable inside an arithmetic expression.
const KS_NUMERIC_TTYPES = ["integer", "float", "monetary"];

// Mirrors the alias rule in KsReportBuilder._ks_compile_expression: a column
// whose technical name starts with "x_" is reachable in an expression under
// its name with that one leading "x_" stripped.
function ksAliasForColumn(columnName) {
    if (!columnName) {
        return "";
    }
    return columnName.startsWith("x_") ? columnName.slice(2) : columnName;
}

export class KsExpressionField extends CharField {
    static template = "ks_report_builder.KsExpressionField";
    static components = { ...CharField.components, Dropdown, DropdownItem };

    /**
     * Read sibling rows straight from the in-memory Columns list on the
     * parent report record (not a fresh RPC), so a column added earlier in
     * the same, still-unsaved editing session is offered immediately - this
     * matches what the Python compiler will see at Deploy time, since that
     * also reads from field_ids as currently written.
     */
    get availableColumns() {
        const fieldIds = this.props.record.model.root.data.field_ids;
        if (!fieldIds) {
            return [];
        }
        return fieldIds.records
            .filter((rec) => rec.data.kind === "path" && KS_NUMERIC_TTYPES.includes(rec.data.ttype))
            .map((rec) => ({
                label: rec.data.label || rec.data.column_name,
                alias: ksAliasForColumn(rec.data.column_name),
            }))
            .filter((col) => col.alias);
    }

    onInsertColumn(alias) {
        const el = this.input.el;
        if (!el) {
            return;
        }
        const start = el.selectionStart ?? el.value.length;
        const end = el.selectionEnd ?? el.value.length;
        el.focus();
        el.setRangeText(alias, start, end, "end");
        el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
}

export const ksExpressionField = {
    ...charField,
    component: KsExpressionField,
    displayName: "Expression (with field picker)",
};

registry.category("fields").add("ks_expression_picker", ksExpressionField);
