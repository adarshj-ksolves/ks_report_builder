from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .ir_model import KS_SNAPSHOT_PREFIX
from .ks_field_path_mixin import ks_slugify


class KsReportSnapshot(models.Model):
    _name = 'ks.report.snapshot'
    _description = 'Snapshot Field'
    _inherit = ['ks.field.path.mixin']
    _order = 'base_model_id, name'

    name = fields.Char(string='Label', required=True)
    base_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Store On Model',
        required=True,
        ondelete='cascade',
        domain="[('transient', '=', False), ('abstract', '=', False)]",
        help="The field is added to this model and filled once, when a record "
             "is created.",
    )
    field_name = fields.Char(
        string='Technical Name', compute='_compute_field_name', store=True)
    state = fields.Selection(
        selection=[('draft', 'Draft'), ('active', 'Active')],
        default='draft', required=True, readonly=True, copy=False)
    generated_field_id = fields.Many2one(
        comodel_name='ir.model.fields', string='Generated Field',
        readonly=True, copy=False, ondelete='set null')
    compute_code = fields.Text(
        string='Generated Code', readonly=True, copy=False,
        help="Generated for you from the dropdowns above. Shown for audit only.")
    backfilled = fields.Boolean(string='Backfilled', readonly=True, copy=False)

    _field_name_uniq = models.Constraint(
        'unique(base_model_id, field_name)',
        "That snapshot field already exists on this model.",
    )

    @api.depends('name', 'path')
    def _compute_field_name(self):
        for snapshot in self:
            slug = ks_slugify(snapshot.path) or ks_slugify(snapshot.name)
            snapshot.field_name = '%s%s' % (KS_SNAPSHOT_PREFIX, slug or 'value')

    @api.constrains('path', 'ttype')
    def _check_source(self):
        for snapshot in self:
            if not snapshot.path:
                raise ValidationError(_("Pick the source field to snapshot."))
            if snapshot.ttype == 'many2one':
                raise ValidationError(_(
                    "Snapshotting a relation is pointless: the link already "
                    "persists. Drill one level further and snapshot a value on "
                    "the related record instead."))

    # ------------------------------------------------------------------
    # Generated compute code
    # ------------------------------------------------------------------

    def _ks_build_compute_code(self):
        """Build the compute body stored on ir.model.fields.

        Executed by Odoo through ``make_compute`` with an EMPTY depends list,
        so it runs when a record is created and is never recomputed. That is
        what makes the value a snapshot rather than a live mirror.
        """
        self.ensure_one()
        segments = self.path.split('.')
        guard = ' and '.join(
            'record.%s' % '.'.join(segments[:index + 1])
            for index in range(len(segments) - 1)
        )
        default = {'char': "''", 'text': "''", 'boolean': 'False'}.get(
            self.ttype, 'False' if self.ttype in ('date', 'datetime') else '0.0')
        expression = 'record.%s' % self.path
        if guard:
            value = '%s if %s else %s' % (expression, guard, default)
        else:
            value = expression
        return "for record in self:\n    record['%s'] = %s\n" % (
            self.field_name, value)

    # ------------------------------------------------------------------
    # Activate / deactivate
    # ------------------------------------------------------------------

    def action_activate(self):
        for snapshot in self:
            if snapshot.state == 'active':
                continue
            target = self.env.get(snapshot.base_model_id.model)
            if target is None or not target._auto:
                raise UserError(_(
                    "%s is not a table-backed model and cannot carry a stored "
                    "field.", snapshot.base_model_id.model))
            if snapshot.field_name in target._fields:
                raise UserError(_(
                    "%(f)s already exists on %(m)s.",
                    f=snapshot.field_name, m=snapshot.base_model_id.model))
            code = snapshot._ks_build_compute_code()
            generated = self.env['ir.model.fields'].sudo().create({
                'name': snapshot.field_name,
                'field_description': snapshot.name,
                'model_id': snapshot.base_model_id.id,
                'ttype': snapshot.ttype,
                'state': 'manual',
                'store': True,
                'readonly': True,
                'copied': False,
                'compute': code,
                'depends': '',
            })
            snapshot.write({
                'generated_field_id': generated.id,
                'compute_code': code,
                'state': 'active',
            })
        return True

    def action_deactivate(self):
        for snapshot in self:
            if snapshot.generated_field_id:
                # Drops the column and its stored history.
                snapshot.generated_field_id.sudo().unlink()
            snapshot.write({'generated_field_id': False, 'state': 'draft',
                            'backfilled': False})
        return True

    def action_backfill(self):
        """Fill existing records using TODAY's source values.

        Historical accuracy is impossible here - the past value was never
        stored. Only records created after activation carry a true snapshot.
        """
        for snapshot in self:
            if snapshot.state != 'active':
                raise UserError(_("Activate the snapshot field first."))
            target = self.env[snapshot.base_model_id.model].sudo()
            field = target._fields[snapshot.field_name]
            records = target.with_context(active_test=False).search([])
            if records:
                self.env.add_to_compute(field, records)
                self.env.flush_all()
            snapshot.backfilled = True
        return True

    def unlink(self):
        self.action_deactivate()
        return super().unlink()
