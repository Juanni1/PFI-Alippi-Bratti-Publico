"""Persistencia: esquema, claves foráneas, UTC y las propiedades del DER que no
se leen del diagrama."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from app.adaptadores.database import (
    create_database_engine,
    create_session_factory,
    session_scope,
    verify_schema,
)
from app.adaptadores.models import Claim, Configuration, Evidence, Transition
from app.dominio.vocabulary import ClaimStatus, PinStatus, TransitionCause




def _reclamo(**cambios) -> Claim:
    argumentos = {
        "tracking_number": "SN-4127",
        "access_token_hash": "a" * 64,
        "declared_amount": Decimal("185000.00"),
        "status": ClaimStatus.REGISTRADO,
    }
    argumentos.update(cambios)
    return Claim(**argumentos)





def test_las_claves_foraneas_se_verifican(sesiones):
    """El modelo relacional del DER verifica la integridad referencial."""
    with pytest.raises(IntegrityError):
        with session_scope(sesiones) as sesion:
            sesion.add(
                Evidence(
                    claim_id=9999,
                    filename="frente.jpg",
                    mime_type="image/jpeg",
                    size_bytes=1024,
                    sha256="b" * 64,
                    cid="bafkreib32b4tivsgh5ll4d65mst2akk6wocdk2rqjuir3xwzsbu5csefoe",
                )
            )


def test_el_numero_de_seguimiento_es_unico(sesiones):
    with session_scope(sesiones) as sesion:
        sesion.add(_reclamo())
    with pytest.raises(IntegrityError):
        with session_scope(sesiones) as sesion:
            sesion.add(_reclamo())


def test_el_reclamo_se_referencia_a_si_mismo_para_la_apelacion(sesiones):
    """La máquina de estados no ofrece vía de reenvío dentro del mismo
    expediente: el recurso del asegurado es un reclamo nuevo enlazado al
    anterior. El campo entra ahora aunque su caso de uso quede fuera del
    prototipo, porque agregarlo después obligaría a una migración."""
    with session_scope(sesiones) as sesion:
        original = _reclamo(status=ClaimStatus.RECHAZADO)
        sesion.add(original)
        sesion.flush()
        sesion.add(_reclamo(tracking_number="SN-4128", origin_claim_id=original.id))

    with session_scope(sesiones) as sesion:
        apelacion = sesion.query(Claim).filter_by(tracking_number="SN-4128").one()
        assert apelacion.origin_claim_id is not None


def test_la_evidencia_registra_la_fijacion_de_los_dos_custodios(sesiones):
    """El nodo propio publica y fija; el servicio remoto computa el CID por su
    cuenta sobre los mismos bytes. Sin los tres campos, RNF-20 no tiene contra
    qué comprobar el segundo origen."""
    with session_scope(sesiones) as sesion:
        reclamo = _reclamo()
        sesion.add(reclamo)
        sesion.flush()
        sesion.add(
            Evidence(
                claim_id=reclamo.id,
                filename="frente.jpg",
                mime_type="image/jpeg",
                size_bytes=1024,
                sha256="b" * 64,
                cid="bafkreib32b4tivsgh5ll4d65mst2akk6wocdk2rqjuir3xwzsbu5csefoe",
                pin_status=PinStatus.FIJADO,
                remote_pin_status=PinStatus.FIJADO,
                remote_cid_verified=True,
            )
        )

    with session_scope(sesiones) as sesion:
        evidencia = sesion.query(Evidence).one()
        assert evidencia.pin_status is PinStatus.FIJADO
        assert evidencia.remote_pin_status is PinStatus.FIJADO
        assert evidencia.remote_cid_verified is True


def test_los_umbrales_sin_medir_se_guardan_nulos(sesiones):
    """Traducción al modelo de la marca [VERIFICAR] de la bóveda: la columna
    admite nulo en lugar de traer un valor por defecto plausible, porque un
    número inventado acá saldría por la API como si estuviera medido."""
    with session_scope(sesiones) as sesion:
        sesion.add(Configuration(version="v0"))

    with session_scope(sesiones) as sesion:
        configuracion = sesion.query(Configuration).one()
        assert configuracion.derivation_threshold is None
        assert configuracion.auto_settlement_threshold is None
        assert configuracion.amount_limit is None
        assert configuracion.max_file_size_bytes is None
        assert configuracion.max_job_attempts is None
        # Los que sí son decisión de diseño y no medición traen su valor.
        assert configuracion.queue_concurrency == 1
        assert configuracion.max_files == 5


def test_la_transicion_queda_atada_a_la_configuracion_vigente(sesiones):
    """Sin esta asociación, un cambio de umbrales vuelve irreproducible toda
    decisión anterior, que es lo contrario de la propiedad de no repudio que el
    proyecto sostiene."""
    with session_scope(sesiones) as sesion:
        reclamo = _reclamo()
        configuracion = Configuration(version="v1", derivation_threshold=0.70)
        sesion.add_all([reclamo, configuracion])
        sesion.flush()
        sesion.add(
            Transition(
                claim_id=reclamo.id,
                configuration_id=configuracion.id,
                from_status=ClaimStatus.REGISTRADO,
                to_status=ClaimStatus.APROBABLE,
                cause=TransitionCause.CLASIFICACION_FAVORABLE,
            )
        )

    with session_scope(sesiones) as sesion:
        transicion = sesion.query(Transition).one()
        assert transicion.configuration.derivation_threshold == 0.70
        assert isinstance(transicion.recorded_at, datetime)


def test_la_entrada_al_sistema_se_asienta_sin_estado_de_origen(sesiones):
    """adr-001 enumera el registro aparte de la matriz: no es una transición y
    por eso el origen va nulo."""
    with session_scope(sesiones) as sesion:
        reclamo = _reclamo()
        configuracion = Configuration(version="v1")
        sesion.add_all([reclamo, configuracion])
        sesion.flush()
        sesion.add(
            Transition(
                claim_id=reclamo.id,
                configuration_id=configuracion.id,
                to_status=ClaimStatus.REGISTRADO,
            )
        )

    with session_scope(sesiones) as sesion:
        transicion = sesion.query(Transition).one()
        assert transicion.from_status is None
        assert transicion.cause is None


def test_los_umbrales_iniciales_salen_de_la_curva_medida(sesiones):
    """adr-014: la configuración inicial ya no nace con los umbrales en nulo,
    porque RNF-15 se midió el 22/08 sobre las 785 muestras de prueba."""
    from app.dominio.claims import current_configuration

    with session_scope(sesiones) as sesion:
        configuracion = current_configuration(sesion)
        assert configuracion.derivation_threshold == 0.90
        assert configuracion.auto_settlement_threshold == 0.95


def test_una_configuracion_con_los_umbrales_invertidos_no_se_deja_correr(sesiones):
    """RF-51. Invertidos, el sistema liquidaría sin intervención casos que no
    alcanzaron la confianza mínima para resolverse solos, que es lo contrario
    de lo que los dos umbrales significan."""
    from app.dominio.claims import InvalidConfiguration, current_configuration

    with session_scope(sesiones) as sesion:
        configuracion = current_configuration(sesion)
        configuracion.auto_settlement_threshold = 0.80  # por debajo del 0,90
        sesion.flush()
        with pytest.raises(InvalidConfiguration, match="RF-51"):
            current_configuration(sesion)


def test_marcas_temporales_sobreviven_rollback_y_roundtrip_no_utc(postgres_url):
    """La zona del servidor no cambia el instante ni la zona de lectura."""
    url = make_url(postgres_url)
    options = f"{url.query['options']} -ctimezone=America/Argentina/Buenos_Aires"
    engine = create_database_engine(
        url.set(query={**url.query, "options": options}).render_as_string(
            hide_password=False
        )
    )
    factory = create_session_factory(engine)
    instant = datetime(2026, 8, 20, 23, 30, 0, 123456, timezone(timedelta(hours=-3)))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
            assert connection.execute(text("SHOW TimeZone")).scalar() == "UTC"
        with session_scope(factory) as session:
            session.add(_reclamo(created_at=instant, updated_at=instant))
        with session_scope(factory) as session:
            claim = session.query(Claim).filter_by(tracking_number="SN-4127").one()
            assert claim.created_at == instant.astimezone(timezone.utc)
            assert claim.created_at.utcoffset() == timedelta(0)
    finally:
        engine.dispose()


def test_startup_rechaza_base_no_migrada(postgres_unmigrated_url, monkeypatch):
    """La API no alcanza un healthcheck sano sin aplicar la revisión."""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", postgres_unmigrated_url)
    with pytest.raises(RuntimeError, match="no está migrada"):
        with TestClient(app):
            pytest.fail("La aplicación inició sin migraciones.")
