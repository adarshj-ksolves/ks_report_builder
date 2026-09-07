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
        if (fieldDef.translate) {
            // translate=True fields (e.g. product.template.name) store a
            // per-language jsonb blob, not a plain scalar column - kept in
            // sync with the server-side rejection in ks_walk_path
            // (models/ks_field_path_mixin.py), which is the actual
            // enforcement; this only keeps them out of the picker so a user
            // doesn't pick one only to hit a save-time error.
            return false;
        }
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

export const ksFieldChainField = {
    ...fieldSelectorField,
    component: KsFieldChainField,
    displayName: "Field Chain",
};

registry.category("fields").add("ks_field_chain_picker", ksFieldChainField);
