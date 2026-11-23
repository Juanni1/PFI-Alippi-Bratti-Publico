#!/usr/bin/env python3
"""Medición PFI-140; solo base aislada y lote público no personal.

Ejemplo: TEST_DATABASE_URL=postgresql+psycopg://…/pfi140_staging uv run python
 scripts/benchmark-queue.py --api https://api.pfi140-staging.example
 --manifest lote.json --worker-pid 123 --output evidencia-c1.json
 --confirm-isolated pfi140-staging --concurrency 1

El manifiesto contiene {"non_personal": true, "batch": [{"path": "…",
"sha256": "64 hex", "amount": "100.00"}, ...]} con exactamente 12 imágenes.
Cada corrida registra el mismo lote tres veces por C. Sin `--concurrency`,
el plan completo exige confirmación interactiva al cambiar de bloque; con
`--concurrency 1` o `2`, ejecuta solo las tres repeticiones del límite ya
configurado. No modifica controles ni adquiere jobs: usa exclusivamente POST /reclamos y
lee JobAttempt/JobDispatch persistidos. La salida omite payloads, tokens, rutas,
identificadores de reclamos, direcciones y errores de servicios externos.
La CPU/RAM corresponde al árbol local de --worker-pid (y --api-pid si se da);
para un servicio remoto ejecute el medidor donde viven esos procesos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def trial_plan(include_three: bool, concurrency: int | None = None) -> list[int]:
    if concurrency is not None:
        return [concurrency] * 3
    return [1] * 3 + [2] * 3 + ([3] * 3 if include_three else [])


def percentile(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p95": None}
    ordered = sorted(values)
    pos = (len(ordered) - 1) * .95
    low = int(pos)
    p95 = ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (pos - low)
    return {"median": round(statistics.median(ordered), 6), "p95": round(p95, 6)}


def interval_evidence(attempts, concurrency: int) -> dict:
    """Cuenta intervalos [inicio, fin), no muestras del estado instantáneo."""
    events = []
    for attempt in attempts:
        if attempt.finished_at is not None:
            events.extend(((attempt.started_at, 1), (attempt.finished_at, -1)))
    events.sort(key=lambda item: (item[0], item[1]))
    active = peak = 0
    overlap = 0.0
    previous = None
    for instant, delta in events:
        if previous is not None and active > 1:
            overlap += (instant - previous).total_seconds()
        active += delta
        peak = max(peak, active)
        previous = instant
    return {"peak_active": peak, "overlap_seconds": round(overlap, 6),
            "limit_respected": peak <= concurrency and len(events) == 2 * len(attempts),
            "complete_intervals": len(events) // 2}


def summarize_trial(concurrency, resolutions, api_latencies, outcomes, duration, intervals, samples):
    result = {"concurrency": concurrency, "jobs": len(outcomes),
              "resolution_seconds": percentile(resolutions), "api_seconds": percentile(api_latencies),
              "capacity_jobs_per_second": round(sum(x == "ok" for x in outcomes) / duration, 6),
              "errors": sum(x != "ok" for x in outcomes),
              "outcomes": {key: outcomes.count(key) for key in ("ok", "error")},
              "resources": {"peak_tree_cpu_percent": round(max((x[0] for x in samples), default=0), 3),
                            "peak_tree_rss_bytes": max((x[1] for x in samples), default=0)},
              "resource_samples": len(samples)}
    result.update(intervals)
    return result


def _proc_snapshot(roots: list[int]):
    """Reúne todos los descendientes; /proc/stat excluye argumentos y entorno."""
    stats = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / 'stat').read_text()
            fields = text[text.rfind(')') + 2:].split()
            stats[int(entry.name)] = (int(fields[1]), int(fields[11]) + int(fields[12]),
                                      int(fields[21]) * os.sysconf('SC_PAGE_SIZE'))
        except (OSError, ValueError, IndexError):
            continue
    selected = set(roots) & stats.keys()
    while True:
        expanded = selected | {pid for pid, (parent, _, _) in stats.items() if parent in selected}
        if expanded == selected:
            break
        selected = expanded
    return sum(stats[pid][1] for pid in selected), sum(stats[pid][2] for pid in selected), selected


def sample_tree(roots, stop, samples):
    previous = None
    ticks = os.sysconf('SC_CLK_TCK')
    while not stop.wait(.5):
        cpu, rss, selected = _proc_snapshot(roots)
        if previous is not None:
            elapsed = time.monotonic() - previous[0]
            samples.append((max(0., (cpu - previous[1]) / ticks / elapsed * 100), rss))
        previous = (time.monotonic(), cpu)
        if not set(roots) <= selected:
            stop.set()


def load_batch(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get('non_personal') is not True or len(manifest.get('batch', [])) != 12:
        raise ValueError('lote inválido')
    batch = []
    for item in manifest['batch']:
        image = Path(item['path']).read_bytes()
        digest = hashlib.sha256(image).hexdigest()
        if digest != item['sha256'] or not (image.startswith(b'\xff\xd8\xff') or image.startswith(b'\x89PNG\r\n\x1a\n')):
            raise ValueError('imagen inválida')
        from decimal import Decimal
        if Decimal(item['amount']) <= 0:
            raise ValueError('monto inválido')
        mime = 'image/png' if image.startswith(b'\x89PNG') else 'image/jpeg'
        batch.append((image, mime, str(item['amount']), digest))
    return batch


def validate_target(args):
    db = os.environ.get('TEST_DATABASE_URL', '')
    api = urlparse(args.api or '')
    database = urlparse(db)
    confirmation = args.confirm_isolated
    api_host = (api.hostname or '').lower()
    database_host = (database.hostname or '').lower()
    database_name = database.path.strip('/').lower()
    loopback = {'localhost', '127.0.0.1'}

    safe_local_api = (
        api_host in loopback and api.scheme in ('https', 'http')
    ) or (
        api_host not in loopback
        and 'staging' in api_host
        and 'production' not in api_host
        and api.scheme == 'https'
    )
    local_target = (
        confirmation in ('pfi93-staging', 'pfi140-staging', 'staging')
        and safe_local_api
        and database_host in loopback
        and any(marker in database_name for marker in ('pfi140', 'staging'))
    )
    railway_target = (
        confirmation == 'staging'
        and os.environ.get('RAILWAY_ENVIRONMENT_NAME') == confirmation
        and bool(database_name)
        and database_host.endswith('.railway.internal')
        and api_host not in loopback
        and 'staging' in api_host
        and 'production' not in api_host
        and api.scheme == 'https'
    )
    if (not (local_target or railway_target) or
        database.scheme not in ('postgresql+psycopg', 'postgresql', 'postgres') or
        args.worker_pid is None or args.worker_pid <= 0 or
        (args.api_pid is not None and args.api_pid <= 0)):
        raise ValueError('Destino aislado requerido; no se modificó la cola.')
    return db


def measure(args, batch, db):
    import httpx
    from sqlalchemy import func, select
    from app.adaptadores.database import create_database_engine, create_session_factory, verify_schema
    from app.adaptadores.models import Claim, Configuration, Job, JobAttempt, JobDispatch
    from app.dominio.queue_acquisition import validate_queue_control
    from app.dominio.vocabulary import JobStatus

    engine = create_database_engine(db)
    verify_schema(engine)
    factory = create_session_factory(engine)
    results = []
    plan = trial_plan(args.include_three, args.concurrency)
    # No se cambia la configuración ni se toma un job por fuera del protocolo.
    with httpx.Client(base_url=args.api, timeout=30) as client:
        for index, c in enumerate(plan):
            if index and c != plan[index - 1]:
                # El operador cambia configuración activa, queue_control y pool
                # fuera del medidor; no se fuerza jamás un segundo protocolo.
                if not sys.stdin.isatty():
                    raise ValueError('Cambio de límite requiere confirmación interactiva.')
                print(f'Configure C={c} en el destino aislado y presione Enter.', file=sys.stderr)
                input()
            with factory() as session:
                control = validate_queue_control(session, pool_concurrency=c)
                if control.paused or session.scalar(select(func.count(Job.id)).where(
                    Job.status.in_((JobStatus.PENDIENTE, JobStatus.EN_CURSO)))):
                    raise ValueError('Cola no disponible o no vacía; no se inició el bloque.')
                config = session.get(Configuration, control.configuration_id)
                config_version = config.version
            stop = threading.Event()
            samples = []
            roots = [args.worker_pid] + ([args.api_pid] if args.api_pid else [])
            if not set(roots) <= _proc_snapshot(roots)[2]:
                raise ValueError('Procesos de medición ausentes; no se inició el bloque.')
            sampler = threading.Thread(target=sample_tree, args=(roots, stop, samples), daemon=True)
            sampler.start()
            jobs = []
            api_times = []
            started = time.monotonic()
            try:
                for image, mime, amount, _ in batch:
                    tick = time.monotonic()
                    response = client.post('/reclamos', files={'photos': ('patch.jpg', image, mime)},
                                           data={'declaredAmount': amount,
                                                 'damageDescription': 'Lote técnico no personal PFI-140'})
                    api_times.append(time.monotonic() - tick)
                    if response.status_code != 201:
                        raise ValueError('Registro HTTP fallido; consultar logs privados.')
                    number = response.json()['number']
                    with factory() as session:
                        job = session.scalars(select(Job).join(Claim).where(Claim.tracking_number == number)).one()
                        if session.get(JobDispatch, job.id) is None:
                            raise ValueError('Falta despacho persistido.')
                        jobs.append(job.id)
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    with factory() as session:
                        completed = session.scalars(select(Job).where(Job.id.in_(jobs))).all()
                        if all(job.finished_at is not None for job in completed):
                            break
                    time.sleep(1)
                else:
                    raise ValueError('Tiempo de espera agotado; revisar el destino aislado.')
                duration = time.monotonic() - started
                with factory() as session:
                    attempts = session.scalars(select(JobAttempt).where(JobAttempt.job_id.in_(jobs))).all()
                    dispatches = session.scalars(select(JobDispatch).where(JobDispatch.job_id.in_(jobs))).all()
                    resolutions = [(job.finished_at - job.enqueued_at).total_seconds() for job in completed]
                    outcomes = ['ok' if job.status == JobStatus.COMPLETADO else 'error' for job in completed]
                    evidence = interval_evidence(attempts, c)
                    if (len(completed) != 12 or len({a.job_id for a in attempts}) != 12 or
                        len(dispatches) != 12 or evidence['complete_intervals'] != len(attempts) or
                        not evidence['limit_respected']):
                        raise ValueError('Intervalos incompletos o límite excedido.')
                    result = summarize_trial(c, resolutions, api_times, outcomes, duration, evidence, samples)
                    result['dispatch_publish_attempts'] = sum(d.publish_attempts for d in dispatches)
                    result['attempts'] = len(attempts)
                    result['configuration_version'] = config_version
                    result['batch_sha256'] = hashlib.sha256(''.join(x[3] for x in batch).encode()).hexdigest()
                    results.append(result)
            finally:
                stop.set()
                sampler.join(timeout=2)
    engine.dispose()
    return {'protocol': 'PFI-140-T2B-v1', 'trials': results,
            'note': 'Recuperación, HTTP/UI y cadena requieren pruebas independientes; esto no mide Railway remotamente.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Medir cola aislada PFI-140 sin cambiar su adquisición.')
    parser.add_argument('--api')
    parser.add_argument('--manifest')
    parser.add_argument('--worker-pid', type=int)
    parser.add_argument('--api-pid', type=int)
    parser.add_argument('--output')
    parser.add_argument('--confirm-isolated')
    plan = parser.add_mutually_exclusive_group()
    plan.add_argument('--concurrency', type=int, choices=(1, 2),
                      help='ejecutar tres repeticiones del límite ya configurado')
    plan.add_argument('--include-three', action='store_true')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        db = validate_target(args)
        if not args.manifest or not args.output or args.timeout <= 0 or Path(args.output).exists():
            raise ValueError('Manifiesto y salida nueva requeridos; no se modificó la cola.')
        batch = load_batch(args.manifest)
        result = measure(args, batch, db)
        with Path(args.output).open('x') as file:
            json.dump(result, file, indent=2, sort_keys=True)
            file.write('\n')
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Los detalles de DB, HTTP y archivos podrían incluir secretos o PII.
        message = str(error) if isinstance(error, ValueError) else 'Medición fallida; consultar logs privados.'
        if message != 'Destino aislado requerido; no se modificó la cola.':
            message = 'Medición fallida; consultar logs privados.'
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
