/** @odoo-module **/

import { registry } from "@web/core/registry";
import {
    FieldSelectorField,
    fieldSelectorField,
} from "@web/views/fields/field_selector/field_selector_field";

// Kept in sync with KS_SCALAR_TYPES / KS_TRAVERSABLE_TYPES in
// models/ks_field_path_mixin.py. Excluding one2many/many2many here enforces
// "many2one traversal only": such a field never appears as a selectable row,
// so it can be neither a column nor a mid-path hop.
const KS_SCALAR_TYPES = new Set([
    "char", "text", "selection", "integer", "float", "monetary",
    "boolean", "date", "datetime", "many2one",
]);

export class KsFieldChainField extends FieldSelectorField {
    filter(fieldDef) {
        if (!KS_SCALAR_TYPES.has(fieldDef.type)) {
            return false;
        }
        // translate=True fields are selectable: the query builder unwraps
        // the per-language jsonb with ->> (see _ks_field_sql).
        // Stored fields resolve directly; _inherits-delegated and related=
        // fields default to store=False but are backed by a real column on
        // the parent, which _ks_resolve_column follows. A field that is
        // neither stored nor related has no column anywhere.
        return Boolean(fieldDef.store) || Boolean(fieldDef.related);
    }

    // Deliberately does NOT auto-fill the Label. Updating a sibling field
    // from this field's own update() raises a server-side "KeyError: 'label'"
    // in web/models/models.py's onchange(), because the onchange spec derived
    // from the view arch does not expect it back in initial_values.
}

// Report COLUMNS additionally allow x2many hops, which the query builder
// collapses to a single value with a correlated subquery (the "When Multiple"
// choice). A separate widget rather than an option on the base one, so
// snapshot fields and the aggregate correlation key keep the stricter
// many2one-only picker.
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
