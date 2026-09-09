/** @odoo-module **/

import { registry } from "@web/core/registry";
import {
    FieldSelectorField,
    fieldSelectorField,
} from "@web/views/fields/field_selector/field_selector_field";

// Kept in sync with KS_SCALAR_TYPES / KS_TRAVERSABLE_TYPES in
// models/ks_field_path_mixin.py. Excluding one2many/many2many here is what
// actually enforces "many2one traversal only" - a field of an excluded type
// never appears as a selectable row in the picker, so it can neither be
// chosen as a column nor drilled into as a mid-path hop.
const KS_SCALAR_TYPES = new Set([
    "char", "text", "selection", "integer", "float", "monetary",
    "boolean", "date", "datetime", "many2one",
]);

export class KsFieldChainField extends FieldSelectorField {
    filter(fieldDef) {
        if (!KS_SCALAR_TYPES.has(fieldDef.type)) {
            return false;
        }
        // NOTE: translate=True fields (product names, tag names) ARE
        // selectable. They are stored as a per-language jsonb blob, which the
        // query builder unwraps with ->> using the report's own Report
        // Language (see _ks_field_sql in models/ks_report_builder.py). They
        // used to be filtered out here, which blocked most of the fields
        // people actually want to report on.
        // Stored fields resolve directly; _inherits-delegated fields (e.g.
        // product.product.list_price, physically on product.template)
        // default to store=False in Odoo 19 even though they're backed by a
        // real column on the parent - KsReportBuilder._ks_resolve_column
        // follows that delegation. A field that is neither stored nor
        // related has no column anywhere and can't be used.
        return Boolean(fieldDef.store) || Boolean(fieldDef.related);
    }

    // NOTE: deliberately does NOT auto-fill the Label from fieldInfo the way
    // the old field_id-based onchange did. Triggering record.update() on a
    // sibling field ("label") from this field's own update() - even as a
    // separate, sequential call - raises a server-side
    // "KeyError: 'label'" in web/models/models.py's onchange(): the onchange
    // spec Odoo derives from the view arch for a change to THIS field
    // doesn't expect "label" to come back in initial_values. Not worth
    // fighting that framework internal for a nice-to-have; the user can type
    // the Label directly, same as every other field.
}

// Report COLUMNS additionally allow one one2many/many2many hop, which the
// query builder collapses to a single value with a correlated subquery (the
// "When Multiple" choice on the column). Kept as a separate widget rather
// than an option on the base one so that snapshot fields and the aggregate
// correlation key - where a list genuinely makes no sense - keep using the
// stricter many2one-only picker and can't be pointed at a list by accident.
const KS_X2MANY_TYPES = new Set(["one2many", "many2many"]);

export class KsFieldChainListField extends KsFieldChainField {
    filter(fieldDef) {
        if (KS_X2MANY_TYPES.has(fieldDef.type)) {
            // Non-stored x2many has no rows to read; the server rejects it
            // too (ks_x2many_sql_info in ks_field_path_mixin.py).
            return Boolean(fieldDef.store);
        }
        return super.filter(fieldDef);
    }
}

export const ksFieldChainField = {
    ...fieldSelectorField,
    component: KsFieldChainField,
    displayName: "Field Chain",
};

export const ksFieldChainListField = {
    ...fieldSelectorField,
    component: KsFieldChainListField,
    displayName: "Field Chain (lists allowed)",
};

registry.category("fields").add("ks_field_chain_picker", ksFieldChainField);
registry.category("fields").add("ks_field_chain_list_picker", ksFieldChainListField);
