"""Los nueve recursos, de extremo a extremo sobre la aplicación real.

Lo que estas pruebas cuidan por encima de todo es el recorte por perfil: que
una celda que la matriz de visibilidad le niega a alguien no aparezca en su
respuesta. Comprobarlo campo por campo es lo que le da sentido a haber escrito
un esquema por perfil en vez de uno solo con todo opcional (RF-46).
"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.adaptadores.database import session_scope
from app.adaptadores.models import Claim, Configuration, Evidence, Job, Transition
from app.dominio.claims import UploadedFile, register_claim, settle_claim
from app.dominio.vocabulary import ClaimStatus
from tests.conftest import DAMAGE_DESCRIPTION, PIXEL_PNG, PublicadorQueFalla

REVISOR = {"Authorization": "Bearer revisor-rev-04"}
OPERADOR = {"Authorization": "Bearer operador-op-02"}


def _reclamante(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_registration_has_no_effects(sesiones, publicador):
    assert publicador.publicados == []
    with session_scope(sesiones) as sesion:
        for model in (Claim, Configuration, Evidence, Job, Transition):
            assert sesion.query(model).count() == 0


# ---------------------------------------------------------------------------
# CU-01 — registrar
# ---------------------------------------------------------------------------


def test_registrar_devuelve_la_constancia_de_mk02(cliente, archivos):
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    )
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()

    assert cuerpo["number"].startswith("SN-")
    assert cuerpo["submitted"]["photoCount"] == 1
    assert cuerpo["submitted"]["damageDescription"] == DAMAGE_DESCRIPTION
    assert cuerpo["accessToken"]
    assert cuerpo["receipt"]["evidenceId"].startswith("bafkrei")
    # La prueba todavía no se generó: el hash viaja nulo y no en cero.
    assert cuerpo["receipt"]["proofHash"] is None


def test_operador_registra_el_relato_con_autor_privado_y_no_falsificable(
    cliente, archivos, sesiones
):
    enviada = "\u00a0 Se dañó 🚙:\r\nCedió el guardabarros.\u0085 "
    esperada = "Se dañó 🚙:\nCedió el guardabarros."
    respuesta = cliente.post(
        "/reclamos",
        headers=OPERADOR,
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": enviada,
            "registeredBy": "operador-falsificado",
        },
    )
    assert respuesta.status_code == 201
    constancia = respuesta.json()
    numero = constancia["number"]
    token = constancia["accessToken"]
    assert constancia["submitted"]["damageDescription"] == esperada
    assert "registeredBy" not in constancia
    assert "registeredBy" not in constancia["submitted"]

    with session_scope(sesiones) as sesion:
        reclamo = sesion.query(Claim).filter_by(tracking_number=numero).one()
        assert reclamo.registered_by == "op-02"
        assert reclamo.damage_description == esperada

    del_revisor = cliente.get(f"/reclamos/{numero}", headers=REVISOR).json()
    del_operador = cliente.get(f"/reclamos/{numero}", headers=OPERADOR).json()
    del_reclamante = cliente.get(
        f"/reclamos/{numero}", headers=_reclamante(token)
    ).json()
    assert del_revisor["registeredBy"] == "op-02"
    assert del_operador["registeredBy"] == "op-02"
    assert del_revisor["damageDescription"] == esperada
    assert del_operador["damageDescription"] == esperada
    assert "registeredBy" not in del_reclamante
    assert "registeredBy" not in del_reclamante["claim"]

    traza_publica = cliente.get(f"/publico/reclamos/{numero}/traza")
    assert traza_publica.status_code == 200
    assert "registeredBy" not in traza_publica.json()


def test_el_registro_rechaza_tokens_invalidos_y_perfiles_no_operador_sin_efectos(
    cliente, archivos, sesiones, publicador, reclamo_registrado
):
    antes_publicados = list(publicador.publicados)
    modelos = (Claim, Configuration, Evidence, Job, Transition)
    with session_scope(sesiones) as sesion:
        antes_filas = tuple(sesion.query(modelo).count() for modelo in modelos)

    solicitudes = [
        ({"Authorization": "Bearer token-invalido"}, 401),
        (REVISOR, 403),
        (_reclamante(reclamo_registrado["token"]), 403),
    ]
    for headers, estado_esperado in solicitudes:
        respuesta = cliente.post(
            "/reclamos",
            headers=headers,
            files=archivos,
            data={
                "declaredAmount": "185000",
                "damageDescription": DAMAGE_DESCRIPTION,
            },
        )
        assert respuesta.status_code == estado_esperado
        assert publicador.publicados == antes_publicados
        with session_scope(sesiones) as sesion:
            despues_filas = tuple(
                sesion.query(modelo).count() for modelo in modelos
            )
        assert despues_filas == antes_filas


def test_un_registro_anonimo_persiste_sin_autor(cliente, archivos, sesiones):
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    )
    assert respuesta.status_code == 201
    with session_scope(sesiones) as sesion:
        reclamo = sesion.query(Claim).filter_by(
            tracking_number=respuesta.json()["number"]
        ).one()
        assert reclamo.registered_by is None

    del_operador = cliente.get(
        f"/reclamos/{respuesta.json()['number']}", headers=OPERADOR
    ).json()
    assert del_operador["registeredBy"] is None


def test_la_constancia_no_expone_el_identificador_tecnico_de_estado(cliente, archivos):
    """RNF-35: el identificador técnico no entra en ninguna respuesta al
    reclamante; lo que ve es la etiqueta redactada."""
    cuerpo = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    ).json()
    assert "statusId" not in cuerpo
    assert cuerpo["status"]["label"] == "Recibido, en análisis"


def test_un_formato_no_admitido_se_rechaza_por_archivo(cliente):
    """RF-02: el rechazo es por archivo y no por envío, para que corregir el que
    falla no obligue a recargar los que pasaron."""
    respuesta = cliente.post(
        "/reclamos",
        files=[
            ("photos", ("frente.png", PIXEL_PNG, "image/png")),
            ("photos", ("presupuesto.pdf", b"%PDF-1.4", "application/pdf")),
        ],
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    )
    assert respuesta.status_code == 422
    cuerpo = respuesta.json()
    assert [r["filename"] for r in cuerpo["rejected"]] == ["presupuesto.pdf"]
    assert cuerpo["rejected"][0]["reason"] == "formato_no_admitido"
    assert cuerpo["acceptedCount"] == 1


def test_un_archivo_sobre_el_limite_de_tamano_se_rechaza(cliente):
    """RNF-10: el control no corría mientras el límite viajaba nulo, porque la
    guarda es condicional sobre él. Con el límite decidido en 10 MB el rechazo
    ocurre, y es por archivo como el de formato."""
    grande = PIXEL_PNG + b"\x00" * (10 * 1024 * 1024)
    respuesta = cliente.post(
        "/reclamos",
        files=[
            ("photos", ("frente.png", PIXEL_PNG, "image/png")),
            ("photos", ("detalle-faro.png", grande, "image/png")),
        ],
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    )
    assert respuesta.status_code == 422
    cuerpo = respuesta.json()
    assert [r["filename"] for r in cuerpo["rejected"]] == ["detalle-faro.png"]
    assert cuerpo["rejected"][0]["reason"] == "excede_tamano_maximo"
    # El mensaje es el de MK-01b: nombra el peso del archivo y el límite, para
    # que quien lo lee sepa cuánto tiene que bajar.
    assert cuerpo["rejected"][0]["message"] == "Pesa 10,0 MB y el máximo es 10 MB."
    assert cuerpo["acceptedCount"] == 1


def test_si_la_publicacion_falla_no_queda_reclamo_a_medias(cliente, archivos, sesiones):
    """Excepción 7a de CU-01: sin número, sin constancia y sin duplicado."""
    from app.adaptadores.models import Claim

    cliente.app.state.publisher = PublicadorQueFalla()
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "1000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    )

    assert respuesta.status_code == 502
    assert respuesta.json()["retryable"] is True
    with session_scope(sesiones) as sesion:
        assert sesion.query(Claim).count() == 0


def test_registrar_requiere_descripcion(cliente, archivos, sesiones, publicador):
    respuesta = cliente.post(
        "/reclamos", files=archivos, data={"declaredAmount": "185000"}
    )
    assert respuesta.status_code == 422
    _assert_registration_has_no_effects(sesiones, publicador)


@pytest.mark.parametrize(
    "damage_description",
    [" \u00a0\u0085\u001c ", "🚙" * 2001],
)
def test_descripcion_invalida_se_rechaza_antes_de_cualquier_efecto(
    cliente, archivos, sesiones, publicador, damage_description
):
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": damage_description,
        },
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["title"] == "La descripción del daño no es válida."
    _assert_registration_has_no_effects(sesiones, publicador)


def test_descripcion_unicode_se_recorta_persiste_y_llega_a_perfiles_autorizados(
    cliente, archivos, sesiones
):
    enviada = "\u00a0 Se dañó 🚙:\nCedió el guardabarros.\u0085 "
    esperada = "Se dañó 🚙:\nCedió el guardabarros."
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={"declaredAmount": "185000", "damageDescription": enviada},
    )
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    numero = cuerpo["number"]
    token = cuerpo["accessToken"]
    assert cuerpo["submitted"]["damageDescription"] == esperada

    with session_scope(sesiones) as sesion:
        reclamo = sesion.query(Claim).filter_by(tracking_number=numero).one()
        assert reclamo.damage_description == esperada

    del_reclamante = cliente.get(
        f"/reclamos/{numero}", headers=_reclamante(token)
    )
    assert del_reclamante.json()["claim"]["damageDescription"] == esperada
    assert cliente.get(f"/reclamos/{numero}", headers=REVISOR).json()[
        "damageDescription"
    ] == esperada
    assert cliente.get(f"/reclamos/{numero}", headers=OPERADOR).json()[
        "damageDescription"
    ] == esperada
    assert cliente.get(f"/reclamos/{numero}").status_code == 401


def test_2000_emojis_cuentan_como_2000_puntos_de_codigo(cliente, archivos):
    descripcion = "🚙" * 2000
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={"declaredAmount": "185000", "damageDescription": descripcion},
    )
    assert respuesta.status_code == 201
    assert respuesta.json()["submitted"]["damageDescription"] == descripcion


@pytest.mark.parametrize("line_ending", ["\r\n", "\r"])
def test_limite_multilinea_se_mide_y_persiste_con_saltos_lf(
    cliente, archivos, line_ending
):
    descripcion = "a" * 1998 + line_ending + "b"
    esperada = "a" * 1998 + "\n" + "b"
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={"declaredAmount": "185000", "damageDescription": descripcion},
    )
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["submitted"]["damageDescription"] == esperada
    detalle = cliente.get(
        f"/reclamos/{cuerpo['number']}",
        headers=_reclamante(cuerpo["accessToken"]),
    )
    assert detalle.json()["claim"]["damageDescription"] == esperada


def test_reclamo_legacy_serializa_descripcion_nula_en_perfiles_autorizados(
    cliente, reclamo_registrado, sesiones
):
    with session_scope(sesiones) as sesion:
        sesion.get(Claim, reclamo_registrado["id"]).damage_description = None

    reclamante = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}",
        headers=_reclamante(reclamo_registrado["token"]),
    ).json()
    revisor = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}", headers=REVISOR
    ).json()
    operador = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}", headers=OPERADOR
    ).json()
    assert reclamante["claim"]["damageDescription"] is None
    assert revisor["damageDescription"] is None
    assert operador["damageDescription"] is None


# ---------------------------------------------------------------------------
# CU-02 — consultar, recortado por perfil (RF-46, RF-48)
# ---------------------------------------------------------------------------


def test_el_reclamante_recibe_su_esquema_sin_celdas_ajenas(cliente, reclamo_registrado):
    respuesta = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}",
        headers=_reclamante(reclamo_registrado["token"]),
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()

    assert cuerpo["status"]["label"]
    assert cuerpo["timeline"]
    assert cuerpo["claim"]["damageDescription"] == DAMAGE_DESCRIPTION
    # Celdas que la matriz le niega al reclamante.
    assert "statusId" not in cuerpo
    assert "inference" not in cuerpo
    assert "evidence" not in cuerpo


def test_las_marcas_de_tiempo_viajan_con_su_huso(cliente, reclamo_registrado):
    """Sin designador de huso, `2026-08-09T17:31:00` se lee como hora local y la
    pantalla dibuja tres horas de más en GMT-3. El repositorio las guarda en UTC
    y la frontera lo declara; la conversión a hora local es de la interfaz."""
    cuerpo = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}",
        headers=_reclamante(reclamo_registrado["token"]),
    ).json()

    marca = cuerpo["timeline"][0]["occurredAt"]
    assert marca.endswith("Z")
    assert datetime.fromisoformat(marca).tzinfo is not None


def test_el_revisor_recibe_el_identificador_tecnico_y_la_imagen(cliente, reclamo_registrado):
    cuerpo = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}", headers=REVISOR
    ).json()
    assert cuerpo["statusId"] == ClaimStatus.REGISTRADO.value
    assert cuerpo["damageDescription"] == DAMAGE_DESCRIPTION
    assert cuerpo["evidence"]["files"][0]["url"]
    assert cuerpo["evidence"]["files"][0]["sha256"]


def test_el_operador_recibe_el_mismo_reclamo_sin_el_vector_de_logits(
    cliente, reclamo_registrado
):
    """Es la celda de la matriz que da sentido a filtrar por campo y no por
    pantalla: el operador ve el reclamo y no ve los logits."""
    del_operador = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}", headers=OPERADOR
    ).json()
    assert del_operador["statusId"] == ClaimStatus.REGISTRADO.value
    assert del_operador["damageDescription"] == DAMAGE_DESCRIPTION
    assert "logits" not in (del_operador.get("inference") or {})
    assert "availableResolutions" not in del_operador


def test_el_token_de_otro_reclamo_no_abre_este(cliente, reclamo_registrado, sesiones, publicador):
    """RF-47: el token es una capacidad emitida para un expediente. Sin esta
    comprobación alcanzaría con conocer otro número de seguimiento."""
    with session_scope(sesiones) as sesion:
        otro = register_claim(
            sesion,
            publicador,
            damage_description=DAMAGE_DESCRIPTION,
            files=[UploadedFile("otra.png", "image/png", b"otros bytes")],
            declared_amount=Decimal("50000"),
        )
        token_ajeno = otro.access_token

    respuesta = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}", headers=_reclamante(token_ajeno)
    )
    assert respuesta.status_code == 401


def test_sin_token_no_se_consulta(cliente, reclamo_registrado):
    assert cliente.get(f"/reclamos/{reclamo_registrado['number']}").status_code == 401


def test_un_numero_inexistente_devuelve_404(cliente):
    assert cliente.get("/reclamos/SN-9999", headers=REVISOR).status_code == 404


# ---------------------------------------------------------------------------
# MK-04 — explicación
# ---------------------------------------------------------------------------


def test_sin_clasificacion_no_hay_explicacion_que_dar(cliente, reclamo_registrado):
    """Excepción 2a de CU-02. MK-03c anuncia la pantalla sin ofrecer acceso."""
    respuesta = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}/explicacion",
        headers=_reclamante(reclamo_registrado["token"]),
    )
    assert respuesta.status_code == 409


# ---------------------------------------------------------------------------
# MK-05 — bandeja
# ---------------------------------------------------------------------------


def test_la_bandeja_es_del_revisor_y_no_del_operador(cliente):
    assert cliente.get("/revision/bandeja", headers=REVISOR).status_code == 200
    assert cliente.get("/revision/bandeja", headers=OPERADOR).status_code == 403


def test_la_bandeja_declara_su_orden_y_trae_la_configuracion(cliente, reclamo_registrado):
    cuerpo = cliente.get("/revision/bandeja", headers=REVISOR).json()
    assert cuerpo["ordering"] == "llegada"
    assert cuerpo["configuration"]["version"]
    # Un reclamo recién registrado no está derivado y no entra a la bandeja.
    assert cuerpo["rows"] == []


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


def test_los_umbrales_medidos_viajan_con_su_valor(cliente, reclamo_registrado):
    """Los dos umbrales de confianza salen de la curva de RNF-15 medida el
    22/08 y se adoptaron en adr-014, así que viajan con `measured: true`."""
    cuerpo = cliente.get(
        "/configuracion", headers=_reclamante(reclamo_registrado["token"])
    ).json()
    assert cuerpo["derivationThreshold"] == {
        "value": 0.90,
        "measured": True,
        "requirement": "RNF-15",
        "unit": None,
    }
    assert cuerpo["autoSettlementThreshold"]["value"] == 0.95
    # RF-51: el de liquidación automática nunca por debajo del de derivación.
    assert (
        cuerpo["autoSettlementThreshold"]["value"]
        >= cuerpo["derivationThreshold"]["value"]
    )


def test_los_valores_de_configuracion_decididos_viajan_con_su_valor(
    cliente, reclamo_registrado
):
    """Los tres que el 2026-08-23 dejaron de ser [VERIFICAR] viajan con su
    número. Eran decisión de configuración y no medición, y mientras nadie los
    hubiera elegido viajaban nulos; elegirlos es lo que los cierra."""
    cuerpo = cliente.get(
        "/configuracion", headers=_reclamante(reclamo_registrado["token"])
    ).json()
    assert cuerpo["maxFileSizeMb"] == {
        "value": 10.0,
        "measured": True,
        "requirement": "RNF-10",
        "unit": "MB",
    }
    assert cuerpo["amountLimit"]["value"] == 1000000.0
    assert cuerpo["maxRetries"]["value"] == 3
    assert cuerpo["maxFiles"]["value"] == 5
    assert all(
        cuerpo[campo]["measured"] is True
        for campo in ("maxFileSizeMb", "amountLimit", "maxRetries", "maxFiles")
    )


def test_lo_que_sigue_sin_acotar_viaja_nulo_y_declarado(cliente, reclamo_registrado):
    """Nunca un número plausible: `value` nulo con `measured: false` y el
    requerimiento que lo cerraría, que es la traducción de la marca
    [VERIFICAR]. La espera de la cola no se cierra eligiendo un número, así que
    sigue nula aunque los tres de configuración ya no lo estén."""
    cuerpo = cliente.get("/cola", headers=OPERADOR).json()
    espera = cuerpo["counters"]["lastPositionWait"]
    assert espera["value"] is None
    assert espera["measured"] is False


# ---------------------------------------------------------------------------
# CU-10 — cola
# ---------------------------------------------------------------------------


def test_la_cola_es_del_operador_y_cuenta_el_job_del_registro(cliente, reclamo_registrado):
    assert cliente.get("/cola", headers=REVISOR).status_code == 403
    cuerpo = cliente.get("/cola", headers=OPERADOR).json()
    assert cuerpo["counters"]["pending"] == 1
    assert cuerpo["concurrency"] == 1
    assert cuerpo["pending"][0]["number"] == reclamo_registrado["number"]
    # La espera del último de la lista es el número que ningún requerimiento
    # acota: RNF-06 mide con la cola vacía.
    assert cuerpo["counters"]["lastPositionWait"]["measured"] is False


def test_la_cola_lista_los_aprobables_para_que_el_operador_llegue_a_liquidar(
    cliente, sesiones, reclamo_registrado
):
    """El operador no tenía por dónde llegar a la liquidación de RF-37: la
    bandeja de RF-36 es del revisor y CU-06 no pide su equivalente."""
    from sqlalchemy import text

    motor = sesiones.kw["bind"]
    with motor.begin() as conexion:
        conexion.execute(
            text("UPDATE claims SET status = 'Aprobable' WHERE id = :id"),
            {"id": reclamo_registrado["id"]},
        )

    cuerpo = cliente.get("/cola", headers=OPERADOR).json()
    assert [fila["number"] for fila in cuerpo["settleable"]] == [
        reclamo_registrado["number"]
    ]
    assert cuerpo["settleable"][0]["declaredAmount"] == 185000.0
    # Sin transición asentada a Aprobable la marca viaja nula: `updated_at`
    # significaría otra cosa.
    assert cuerpo["settleable"][0]["approvableSince"] is None
    # Listarlos no los convierte en trabajos de la cola.
    assert cuerpo["counters"]["pending"] == 1


def test_la_lista_de_aprobables_es_del_operador(cliente):
    assert cliente.get("/cola", headers=REVISOR).status_code == 403


# ---------------------------------------------------------------------------
# CU-05 y CU-06 — resolución y liquidación
# ---------------------------------------------------------------------------


def test_no_se_resuelve_un_reclamo_que_no_esta_derivado(cliente, reclamo_registrado):
    """RF-31: la matriz no admite ir de Registrado a un terminal."""
    respuesta = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/resolucion",
        headers=REVISOR,
        json={"favorable": True, "justification": "Daño compatible."},
    )
    assert respuesta.status_code == 409


def test_la_justificacion_es_obligatoria(cliente, reclamo_registrado):
    respuesta = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/resolucion",
        headers=REVISOR,
        json={"favorable": True, "justification": ""},
    )
    assert respuesta.status_code == 422


def test_la_liquidacion_es_del_operador(cliente, reclamo_registrado):
    respuesta = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/liquidacion", headers=REVISOR
    )
    assert respuesta.status_code == 403


def test_liquidar_desde_aprobable_asienta_el_camino_del_operador(
    cliente, reclamo_registrado, sesiones
):
    """adr-007: de la causa depende el texto que ven el reclamante y el auditor."""
    from app.adaptadores.models import Claim

    with session_scope(sesiones) as sesion:
        reclamo = sesion.get(Claim, reclamo_registrado["id"])
        reclamo.status = ClaimStatus.APROBABLE

    respuesta = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/liquidacion", headers=OPERADOR
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["statusId"] == ClaimStatus.APROBADO.value
    assert cuerpo["transition"]["cause"] == "LiquidacionDelOperador"
    # Sin respaldo en la cadena todavía: el nulo es la marca de eso y no se
    # rellena con un valor plausible.
    assert cuerpo["transition"]["transactionHash"] is None


def test_un_reclamo_ya_liquidado_no_se_vuelve_a_liquidar(
    cliente, reclamo_registrado, sesiones
):
    """RF-40: los estados terminales no tienen transiciones de salida."""
    from app.adaptadores.models import Claim

    with session_scope(sesiones) as sesion:
        sesion.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.APROBABLE
    cliente.post(f"/reclamos/{reclamo_registrado['number']}/liquidacion", headers=OPERADOR)

    segunda = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/liquidacion", headers=OPERADOR
    )
    assert segunda.status_code == 409


def test_resolver_registra_el_hash_y_no_el_texto_en_la_transicion(
    cliente, reclamo_registrado, sesiones
):
    """RNF-24: a la cadena va el digesto; el texto, la identidad y la marca
    temporal quedan del lado de la aseguradora."""
    from app.adaptadores.models import Claim, Review

    with session_scope(sesiones) as sesion:
        sesion.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.REQUIERE_REVISION

    respuesta = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/resolucion",
        headers=REVISOR,
        json={"favorable": True, "justification": "El daño es compatible con lo declarado."},
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["transition"]["cause"] == "ResolucionFavorable"

    with session_scope(sesiones) as sesion:
        revision = sesion.query(Review).one()
        assert revision.justification_hash.startswith("0x")
        assert revision.reviewer_identity == "rev-04"


def test_el_reclamante_ve_el_rol_del_revisor_y_nunca_su_identidad(
    cliente, reclamo_registrado, sesiones
):
    from app.adaptadores.models import Claim

    with session_scope(sesiones) as sesion:
        sesion.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.REQUIERE_REVISION
    cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/resolucion",
        headers=REVISOR,
        json={"favorable": False, "justification": "No corresponde cobertura."},
    )

    cuerpo = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}",
        headers=_reclamante(reclamo_registrado["token"]),
    ).json()
    assert cuerpo["resolution"]["role"] == "analista"
    assert "reviewer" not in cuerpo["resolution"]

    del_revisor = cliente.get(
        f"/reclamos/{reclamo_registrado['number']}", headers=REVISOR
    ).json()
    assert del_revisor["resolution"]["reviewer"] == "rev-04"


# ---------------------------------------------------------------------------
# CU-08 — traza pública
# ---------------------------------------------------------------------------


def test_la_traza_publica_no_pide_token(cliente, reclamo_registrado):
    """RF-44: sin acreditación de titularidad. Que responda 409 y no 401 es lo
    que prueba que la autorización no se interpuso."""
    respuesta = cliente.get(
        f"/publico/reclamos/{reclamo_registrado['number']}/traza"
    )
    assert respuesta.status_code == 409


def test_la_traza_de_un_numero_inexistente_devuelve_404(cliente):
    assert cliente.get("/publico/reclamos/SN-9999/traza").status_code == 404


@pytest.mark.parametrize("unset_limits", [False, True])
def test_public_configuration_is_minimal_and_read_only(
    cliente, reclamo_registrado, sesiones, unset_limits
):
    if unset_limits:
        with session_scope(sesiones) as session:
            configuration = session.query(Configuration).one()
            configuration.max_file_size_bytes = None
            configuration.amount_limit = None
    headers = _reclamante(reclamo_registrado["token"])
    claim_path = f"/reclamos/{reclamo_registrado['number']}"
    before_claim = cliente.get(claim_path, headers=headers).json()
    before_queue = cliente.get("/cola", headers=OPERADOR).json()
    protected = cliente.get("/configuracion", headers=headers)
    assert protected.status_code == 200

    response = cliente.get("/configuracion/publica")
    assert response.status_code == 200
    expected_keys = {"maxFiles", "maxFileSizeMb", "amountLimit", "acceptedFormats"}
    assert set(response.json()) == expected_keys
    assert response.json() == {key: protected.json()[key] for key in expected_keys}
    if unset_limits:
        for key in ("maxFileSizeMb", "amountLimit"):
            assert response.json()[key]["value"] is None
            assert response.json()[key]["measured"] is False
    assert cliente.get(claim_path, headers=headers).json() == before_claim
    assert cliente.get("/cola", headers=OPERADOR).json() == before_queue


def test_protected_configuration_still_requires_capability_or_staff(cliente, reclamo_registrado):
    headers = _reclamante(reclamo_registrado["token"])
    before = cliente.get("/configuracion", headers=headers)
    assert before.status_code == 200
    before_queue = cliente.get("/cola", headers=OPERADOR).json()
    rejected = cliente.get("/configuracion")
    assert rejected.status_code == 401
    assert rejected.json()["title"] == "No se pudo acreditar el acceso a este reclamo."
    assert cliente.get("/configuracion", headers=headers).json() == before.json()
    assert cliente.get("/configuracion", headers=OPERADOR).json() == before.json()
    assert cliente.get("/cola", headers=OPERADOR).json() == before_queue


def test_public_configuration_is_available_before_registering_a_claim(cliente):
    first = cliente.get("/configuracion/publica")
    assert first.status_code == 200
    assert set(first.json()) == {
        "maxFiles", "maxFileSizeMb", "amountLimit", "acceptedFormats"
    }
    assert first.json()["maxFiles"]["value"] == 5
    assert first.json()["maxFileSizeMb"]["value"] == 10
    assert first.json()["amountLimit"]["value"] == 1000000
    assert first.json()["acceptedFormats"] == ["image/jpeg", "image/png"]
    assert cliente.get("/configuracion/publica").json() == first.json()
    rejected = cliente.get("/configuracion")
    assert rejected.status_code == 401
    assert rejected.json()["title"] == "No se pudo acreditar el acceso a este reclamo."
