"""CLI de reconciliación acotada de operaciones del oráculo (ADR-018).

Permite reconciliar operaciones pendientes o inciertas sin ejecutar sondeos
periódicos en el proceso de la API.

Uso:
    python -m app.adaptadores.reconcile [--operation-id <UUID>] [--limit <N>]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from uuid import UUID

from sqlalchemy import select

from app.adaptadores.chain import build_chain_client
from app.adaptadores.database import create_database_engine, create_session_factory, session_scope
from app.adaptadores.oracle_coordinator import OracleCoordinator
from app.adaptadores.models import Claim
from app.dominio.claims import current_configuration

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reconcile")


def build_coordinator(session_factory) -> OracleCoordinator:
    with session_scope(session_factory) as session:
        config = current_configuration(session)
        threshold = config.derivation_threshold
        policy = config.policy_mode
    chain_client = build_chain_client(derivation_threshold=threshold, policy_mode=policy)
    return OracleCoordinator(session_factory, chain_client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconciliación acotada de transacciones del oráculo")
    parser.add_argument(
        "--operation-id",
        type=str,
        default=None,
        help="UUID de una operación puntual a reconciliar",
    )
    parser.add_argument(
        "--limit",
        "--max-operations",
        type=int,
        default=50,
        dest="limit",
        help="Límite superior de operaciones a procesar en esta ejecución acotada (por defecto: 50)",
    )
    args = parser.parse_args(argv)

    try:
        engine = create_database_engine()
        session_factory = create_session_factory(engine)
        coordinator = build_coordinator(session_factory)

        if args.operation_id:
            op_uuid = UUID(args.operation_id)
            logger.info("Reconciliando operación %s...", op_uuid)
            op = coordinator.reconcile_operation(op_uuid)
            # El coordinador devuelve una operación desacoplada: leer solo el escalar
            # dentro de una sesión evita disparar la carga diferida de op.claim.
            with session_scope(session_factory) as session:
                number = session.scalar(
                    select(Claim.tracking_number).where(Claim.id == op.claim_id)
                )
            res = {
                "operationId": str(op.id),
                "number": number if number is not None else str(op.claim_id),
                "operation": op.operation,
                "status": op.status,
                "transactionHash": op.transaction_hash,
                "blockNumber": op.block_number,
                "lastErrorCode": op.last_error_code,
            }
            print(json.dumps(res, indent=2))
        else:
            logger.info("Buscando hasta %d operaciones pendientes o inciertas...", args.limit)
            ops = coordinator.reconcile_pending(max_operations=args.limit)
            logger.info("Se procesaron %d operaciones.", len(ops))
            summary = [
                {
                    "operationId": str(op.id),
                    "operation": op.operation,
                    "status": op.status,
                    "transactionHash": op.transaction_hash,
                    "blockNumber": op.block_number,
                    "lastErrorCode": op.last_error_code,
                }
                for op in ops
            ]
            print(json.dumps(summary, indent=2))
        return 0
    except Exception as exc:
        logger.error("Fallo durante la reconciliación: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
