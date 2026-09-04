import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

KS_MODEL_PREFIX = 'x_ks_report_'
KS_SNAPSHOT_PREFIX = 'x_snap_'
KS_EMPTY_QUERY = 'SELECT NULL::integer AS id WHERE FALSE'
KS_NUMERIC_TYPES = ('integer', 'float', 'monetary')


def ks_is_report_model(model_name):
    return bool(model_name) and model_name.startswith(KS_MODEL_PREFIX)


def ks_is_snapshot_field(field_name):
    return bool(field_name) and field_name.startswith(KS_SNAPSHOT_PREFIX)


class IrModel(models.Model):
    _inherit = 'ir.model'

    @api.model
    def _ks_read_report_query(self, model_name):
        """Fetch the compiled query for a generated report model.

        Deliberately uses raw SQL: this runs during registry setup, including at
        server boot, when going through the ORM for ks.report.builder is not safe.
        """
        cr = self.env.cr
        cr.execute("""
            SELECT to_regclass('ks_report_builder') IS NOT NULL
        """)
        if not cr.fetchone()[0]:
            return None
        cr.execute("""
            SELECT query
              FROM ks_report_builder
             WHERE model_name = %s
               AND query IS NOT NULL
             LIMIT 1
        """, (model_name,))
        row = cr.fetchone()
        return row[0] if row else None

    @api.model
    def _instanciate_attrs(self, model_data):
        attrs = super()._instanciate_attrs(model_data)
        model_name = model_data.get('model')
        if not ks_is_report_model(model_name):
            return attrs
        query = None
        try:
            query = self._ks_read_report_query(model_name)
        except Exception:  # noqa: BLE001 - registry setup must never hard-fail
            _logger.exception("KS Report Builder: cannot load query for %s", model_name)
        attrs.update({
            '_auto': False,
            '_log_access': False,
            '_table_query': query or KS_EMPTY_QUERY,
        })
        if not query:
            _logger.warning(
                "KS Report Builder: %s has no compiled query, serving an empty set.",
                model_name,
            )
        return attrs


class IrModelFields(models.Model):
    _inherit = 'ir.model.fields'

    @api.model
    def _instanciate_attrs(self, field_data):
        attrs = super()._instanciate_attrs(field_data)
        if attrs is None:
            return attrs
        if ks_is_report_model(field_data.get('model')):
            # Generated columns are never writable and numeric ones must show
            # up as pivot measures. ir.model.fields has no `aggregator`
            # column, so it is injected here rather than stored.
            attrs['readonly'] = True
            if field_data.get('ttype') in KS_NUMERIC_TYPES:
                attrs.setdefault('aggregator', 'sum')
        elif ks_is_snapshot_field(field_data.get('name')):
            # A snapshot must capture the value from THIS record's own
            # create() vals, not from a later recompute - and a field with an
            # empty `depends` has no trigger edges, so Odoo's normal
            # dependency-based recompute never fires it for new records (only
            # explicit backfill does). `precompute` runs the compute against a
            # virtual record built from the create() vals before the INSERT,
            # which is the only reliable way to capture "value as of
            # creation". ir.model.fields has no `precompute` column, so it is
            # injected here rather than stored.
            attrs['precompute'] = True
        return attrs
