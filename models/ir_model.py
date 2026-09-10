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


def ks_read_report_column_kind(cr, model_name, column_name):
    """Fetch ``(kind, agg_base_path)`` for one generated report column.

    Raw SQL: the ORM for ks.report.builder.field is not usable this early in
    registry setup.
    """
    cr.execute("""
        SELECT to_regclass('ks_report_builder_field') IS NOT NULL
    """)
    if not cr.fetchone()[0]:
        return None, None
    cr.execute("""
        SELECT f.kind, f.agg_base_path
          FROM ks_report_builder_field f
          JOIN ks_report_builder r ON r.id = f.report_id
         WHERE r.model_name = %s
           AND f.column_name = %s
         LIMIT 1
    """, (model_name, column_name))
    row = cr.fetchone()
    return (row[0], row[1]) if row else (None, None)


class IrModel(models.Model):
    _inherit = 'ir.model'

    @api.model
    def _ks_read_report_query(self, model_name):
        """Fetch the compiled query for a generated report model.

        Raw SQL: runs during registry setup, when the ORM is not usable.
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
            # ir.model.fields has no `aggregator` column, so it is injected
            # here rather than stored.
            attrs['readonly'] = True
            if field_data.get('ttype') in KS_NUMERIC_TYPES:
                # An aggregate column correlated on a SHARED dimension
                # (agg_base_path set) repeats the same value on every base row
                # with that key, so 'sum' multiplies it; 'avg' returns it
                # unchanged when grouped by that dimension. A self-correlated
                # one (empty agg_base_path) keys on base.id, so it is a
                # genuine per-row measure and must sum, like path/expression
                # columns.
                kind = agg_base_path = None
                try:
                    kind, agg_base_path = ks_read_report_column_kind(
                        self.env.cr, field_data.get('model'), field_data.get('name'))
                except Exception:  # noqa: BLE001 - registry setup must never hard-fail
                    _logger.exception(
                        "KS Report Builder: cannot read column kind for %s.%s",
                        field_data.get('model'), field_data.get('name'))
                if kind == 'aggregate' and agg_base_path:
                    attrs['aggregator'] = 'avg'
                else:
                    attrs.setdefault('aggregator', 'sum')
        elif ks_is_snapshot_field(field_data.get('name')):
            # An empty `depends` gives the field no trigger edges, so a
            # normal recompute never fires on create. `precompute` runs the
            # compute against a virtual record built from the create() vals
            # before the INSERT, capturing the value as of creation.
            # Injected here: ir.model.fields has no `precompute` column.
            attrs['precompute'] = True
        return attrs
