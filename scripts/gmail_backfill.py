"""
ASC-3: backfill de movimientos de Ascent desde Gmail (ver agents/gmail_backfill.py).

Uso (dentro del contenedor de daniel15k-agents, con su entorno):
  python scripts/gmail_backfill.py --account-id 42 --no-llm      # solo conteos y costo estimado, sin LLM
  python scripts/gmail_backfill.py --account-id 42 --dry-run     # corre el agente, no escribe nada
  python scripts/gmail_backfill.py --account-id 42               # corrida real

Imprime solo conteos y totales agregados por mes. Nunca contenido de correos, asuntos, remitentes,
conceptos, montos individuales ni tokens.

Modos de la oficina (sin LLM; ver agents/gmail_office.py). Salida: JSON Lines solo por stdout, nada a
disco ni a logs; los lee el agente de la oficina, que decide cada transacción:
  python scripts/gmail_backfill.py --extract-headers --account-id 42 --since 2026-09-17 --until 2026-09-30 [--only-new]
  python scripts/gmail_backfill.py --extract-bodies --account-id 42 --ids ID1,ID2 [--max-chars 1500]
  python scripts/gmail_backfill.py --extract-bodies --account-id 42 --day-context 2026-09-17 --categories
  python scripts/gmail_backfill.py --hints --account-id 42 --merchants "COMERCIO A|COMERCIO B"
  python scripts/gmail_backfill.py --apply-decisions --account-id 42 [--dry-run] < decisiones.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXIT_BAD_ARGS = 2
EXIT_DEMO = 4
EXIT_GMAIL = 3

COLUMNS = [
    ("emails_reviewed", "revisados"),
    ("excluded", "excluidos"),
    ("already_registered", "ya_reg"),
    ("guard_email", "guarda_correo"),
    ("unreadable", "ilegibles"),
    ("sent_to_llm", "al_llm"),
    ("financial", "financieros"),
    ("created", "creadas"),
    ("pending", "pending"),
    ("guard_tool", "guarda_tool"),
    ("duplicate_id", "duplicadas"),
    ("no_movement", "sin_mov"),
    ("errors", "errores"),
    ("skipped_by_cost", "fuera_tope"),
]


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"fecha inválida (YYYY-MM-DD): {value}")


def parse_exclude_dates(values: list[str]) -> set[date]:
    """Acepta 'YYYY-MM-DD' y rangos 'YYYY-MM-DD..YYYY-MM-DD', separados por coma; repetible."""
    from agents.gmail_backfill import daterange

    out: set[date] = set()
    for value in values or []:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if ".." in item:
                a, b = item.split("..", 1)
                out.update(daterange(_parse_day(a), _parse_day(b)))
            else:
                out.add(_parse_day(item))
    return out


def read_exclude_ids(path: str | None) -> set[str]:
    """Un id de Gmail por línea; '#' inicia comentario."""
    if not path:
        return set()
    ids = set()
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    return ids


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ASC-3: backfill de movimientos desde Gmail (solo conteos).")
    p.add_argument("--account-id", required=True, type=int)
    p.add_argument("--since", type=_parse_day, help="YYYY-MM-DD (por defecto: día siguiente al último movimiento gmail)")
    p.add_argument("--until", type=_parse_day, help="YYYY-MM-DD (por defecto: hoy, hora de Colombia)")
    p.add_argument("--dry-run", action="store_true", help="corre el agente sin ejecutar ninguna escritura")
    p.add_argument("--no-llm", action="store_true",
                   help="no llama al LLM: solo lee Gmail y la API, cuenta y estima el costo (costo 0)")
    p.add_argument("--exclude-dates", action="append", default=[],
                   help="días a dejar fuera: YYYY-MM-DD o YYYY-MM-DD..YYYY-MM-DD, separados por coma")
    p.add_argument("--exclude-ids-file", help="archivo con ids de Gmail a dejar fuera, uno por línea")
    p.add_argument("--max-cost-usd", type=float,
                   help="procesa del día más reciente hacia atrás hasta este costo estimado")
    p.add_argument("--bank-senders-only", action="store_true",
                   help="restringe la consulta de Gmail a los bank_senders configurados (como el nocturno)")
    p.add_argument("--no-summary", action="store_true", help="no envía el resumen final a la app")

    office = p.add_argument_group("modos de la oficina (JSON Lines por stdout, sin LLM)")
    modes = office.add_mutually_exclusive_group()
    modes.add_argument("--extract-headers", action="store_true", help="un renglón por correo del rango con su estado")
    modes.add_argument("--extract-bodies", action="store_true",
                       help="texto de los correos de --ids, y/o --day-context y --categories")
    modes.add_argument("--hints", action="store_true", help="get_classification_hints por cada comercio de --merchants")
    modes.add_argument("--apply-decisions", action="store_true", help="aplica las decisiones JSON leídas de stdin")
    office.add_argument("--only-new", action="store_true", help="--extract-headers: solo los correos new")
    office.add_argument("--ids", default="", help="--extract-bodies: ids de Gmail separados por coma (máx. 25)")
    office.add_argument("--max-chars", type=int, default=1500, help="--extract-bodies: largo máximo del texto")
    office.add_argument("--day-context", type=_parse_day, help="transacciones ya registradas ese día")
    office.add_argument("--categories", action="store_true", help="salida de get_categories")
    office.add_argument("--merchants", default="", help="--hints: comercios separados por |")
    return p


def print_report(result, *, provider_name: str, model: str) -> None:
    from agents.gmail_backfill import (DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M, DEEPSEEK_INPUT_USD_PER_M,
                                       DEEPSEEK_OUTPUT_USD_PER_M)

    mode = "no-llm (estimación)" if result.no_llm else ("dry-run" if result.dry_run else "real")
    print(f"modo: {mode} | proveedor: {provider_name} | modelo: {model}")
    print(f"periodo: {result.since.isoformat()} .. {result.until.isoformat()}")
    header = ["mes"] + [label for _k, label in COLUMNS]
    rows = [[m] + [str(stats[k]) for k, _l in COLUMNS] for m, stats in sorted(result.months.items())]
    totals = result.totals()
    rows.append(["TOTAL"] + [str(totals[k]) for k, _l in COLUMNS])
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    for r in [header] + rows:
        print("  ".join(cell.rjust(w) for cell, w in zip(r, widths)))
    kind = "estimados" if result.tokens_are_estimate else "reportados por el proveedor"
    print(f"tokens LLM ({kind}): entrada={result.prompt_tokens} (cache hit={result.cache_hit_tokens}) "
          f"salida={result.completion_tokens}")
    print(f"costo LLM: {result.cost_usd:.4f} USD (precios USD/M: entrada {DEEPSEEK_INPUT_USD_PER_M}, "
          f"cache hit {DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M}, salida {DEEPSEEK_OUTPUT_USD_PER_M})")
    print(f"costo estimado previo (heurística) de lo seleccionado: {result.estimated_cost_usd:.4f} USD")
    if result.cost_reached_date is not None:
        print(f"tope de costo: se procesó desde {result.cost_reached_date.isoformat()} hasta {result.until.isoformat()}")
    if result.stopped_by_cost_at is not None:
        print(f"tope de costo alcanzado con el costo real: se detuvo en {result.stopped_by_cost_at.isoformat()}")
    if result.goal_linked_by_api is not None:
        print(f"creadas que la API ligó a una meta de ahorro: {result.goal_linked_by_api}")


def main(argv: list[str] | None = None, **kw) -> int:
    # Sin logs de módulos: algunos (heredados del modo en tiempo real) incluyen conceptos y montos.
    previous = logging.root.manager.disable
    logging.disable(logging.ERROR)
    try:
        return _main(argv, **kw)
    finally:
        logging.disable(previous)


def _main(argv: list[str] | None = None, *, deps_factory=None, api_factory=None, token_getter=None,
          today: date | None = None, stdin=None) -> int:
    args = build_parser().parse_args(argv)
    if args.extract_headers or args.extract_bodies or args.hints or args.apply_decisions \
            or args.day_context or args.categories:
        return _office_main(args, deps_factory=deps_factory, api_factory=api_factory,
                            token_getter=token_getter, stdin=stdin)
    if args.dry_run and args.no_llm:
        print("error: --dry-run y --no-llm son excluyentes", file=sys.stderr)
        return EXIT_BAD_ARGS

    from adapters.rails_http import RailsHttpAdapter
    from agents import gmail_backfill as gb
    from agents.gmail_push import COLOMBIA_TZ, _get_gmail_token

    api_factory = api_factory or RailsHttpAdapter
    token_getter = token_getter or _get_gmail_token
    deps_factory = deps_factory or gb.default_deps
    today = today or datetime.now(COLOMBIA_TZ).date()

    # Seguridad: nunca sobre la cuenta demo. Si no se puede comprobar, no corre.
    try:
        demo_id = api_factory().get_demo_account_id()
    except Exception as exc:
        print(f"error: no se pudo resolver la cuenta demo ({type(exc).__name__}); no se corre", file=sys.stderr)
        return EXIT_DEMO
    if not demo_id:
        print("error: la API no devolvió la cuenta demo; no se corre", file=sys.stderr)
        return EXIT_DEMO
    if str(args.account_id) == str(demo_id):
        print("error: --account-id es la cuenta demo; el backfill no corre sobre ella", file=sys.stderr)
        return EXIT_DEMO

    # Periodo
    until = args.until or today
    if args.since:
        since = args.since
        print(f"periodo: --since explícito {since.isoformat()}")
    else:
        try:
            last = gb.last_gmail_transaction_date(args.account_id, today)
        except Exception as exc:
            print(f"error: no se pudo leer el último movimiento gmail ({type(exc).__name__})", file=sys.stderr)
            return EXIT_BAD_ARGS
        since, _until, capped = gb.default_period(last, today)
        print(f"último movimiento source=gmail antes de hoy: {last.isoformat() if last else 'ninguno en 13 meses'}")
        print(f"periodo por defecto: desde {since.isoformat()}" + (f" (recortado a {gb.MAX_DAYS} días)" if capped else ""))
    if (until - since).days + 1 > gb.MAX_DAYS:
        since = until - timedelta(days=gb.MAX_DAYS - 1)
        print(f"periodo recortado a {gb.MAX_DAYS} días: desde {since.isoformat()}")
    if since > until:
        print("nada que hacer: el periodo está vacío")
        return 0

    access_token, bank_senders = token_getter(args.account_id)
    if not access_token:
        print("error: no hay token de Gmail para la cuenta (reconectar Gmail en la app)", file=sys.stderr)
        return EXIT_GMAIL
    extra_query = ""
    if args.bank_senders_only:
        if not bank_senders:
            print("error: --bank-senders-only pero la cuenta no tiene bank_senders configurados", file=sys.stderr)
            return EXIT_BAD_ARGS
        extra_query = "(" + " OR ".join(f"from:{s}" for s in bank_senders) + ")"

    deps = deps_factory(args.account_id, access_token, extra_query=extra_query)
    if args.no_summary:
        deps.send_summary = None
    del access_token

    try:
        result = gb.run_backfill(
            args.account_id,
            gb.daterange(since, until),
            deps,
            dry_run=args.dry_run,
            no_llm=args.no_llm,
            excluded_ids=read_exclude_ids(args.exclude_ids_file),
            excluded_dates=parse_exclude_dates(args.exclude_dates),
            max_cost_usd=args.max_cost_usd,
        )
    except gb.httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        host = exc.request.url.host if exc.request is not None else "?"
        print(f"error HTTP {status} en {host}; si es Gmail (401/403), reconectar Gmail en la app", file=sys.stderr)
        return EXIT_GMAIL

    if not args.dry_run and not args.no_llm and result.created_ids:
        try:
            months = sorted({(d.year, d.month) for d in gb.daterange(since, until)})
            result.goal_linked_by_api = gb.count_goal_linked(args.account_id, result.created_ids, months)
        except Exception:
            result.goal_linked_by_api = None

    from services.llm_factory import resolve_llm_provider_name

    try:
        provider_name = resolve_llm_provider_name()
    except Exception:
        provider_name = "?"
    print_report(result, provider_name=provider_name, model=deps.model or "?")
    return 0


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)


def _office_main(args, *, deps_factory, api_factory, token_getter, stdin) -> int:
    """Modos de la oficina. Todo sale como JSON Lines por stdout; los errores, como {"error": ...}."""
    from adapters.rails_http import RailsHttpAdapter
    from agents import gmail_backfill as gb
    from agents import gmail_office as go
    from agents.gmail_push import _get_gmail_token

    api_factory = api_factory or RailsHttpAdapter
    token_getter = token_getter or _get_gmail_token
    deps_factory = deps_factory or (lambda a, t, extra_query="": gb.default_deps(a, t, extra_query=extra_query,
                                                                                    llm=False))

    # Validación de argumentos antes de tocar la red.
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    merchants = [m.strip() for m in args.merchants.split("|") if m.strip()]
    if args.extract_headers and not (args.since and args.until):
        _emit({"error": "--extract-headers requiere --since y --until"})
        return EXIT_BAD_ARGS
    if args.extract_headers and (args.since > args.until or (args.until - args.since).days + 1 > gb.MAX_DAYS):
        _emit({"error": f"rango inválido (máximo {gb.MAX_DAYS} días)"})
        return EXIT_BAD_ARGS
    if len(ids) > go.MAX_BODY_IDS:
        _emit({"error": f"máximo {go.MAX_BODY_IDS} ids por llamada"})
        return EXIT_BAD_ARGS
    if args.extract_bodies and not (ids or args.day_context or args.categories):
        _emit({"error": "--extract-bodies requiere --ids, --day-context o --categories"})
        return EXIT_BAD_ARGS
    if args.hints and not merchants:
        _emit({"error": "--hints requiere --merchants"})
        return EXIT_BAD_ARGS
    if args.max_chars <= 0:
        _emit({"error": "--max-chars debe ser positivo"})
        return EXIT_BAD_ARGS
    decisions = None
    if args.apply_decisions:
        try:
            decisions = json.load(stdin or sys.stdin)
        except ValueError:
            _emit({"error": "stdin no es JSON válido"})
            return EXIT_BAD_ARGS
        if not isinstance(decisions, list):
            _emit({"error": "stdin debe ser una lista JSON de decisiones"})
            return EXIT_BAD_ARGS

    # Seguridad: nunca sobre la cuenta demo. Si no se puede comprobar, no corre.
    try:
        demo_id = api_factory().get_demo_account_id()
    except Exception as exc:
        _emit({"error": f"no se pudo resolver la cuenta demo ({type(exc).__name__}); no se corre"})
        return EXIT_DEMO
    if not demo_id or str(args.account_id) == str(demo_id):
        _emit({"error": "cuenta demo o no resuelta; no se corre"})
        return EXIT_DEMO

    needs_gmail = args.extract_headers or args.apply_decisions or bool(ids)
    access_token, bank_senders = (token_getter(args.account_id) if needs_gmail else (None, []))
    if needs_gmail and not access_token:
        _emit({"error": "no hay token de Gmail para la cuenta (reconectar Gmail en la app)"})
        return EXIT_GMAIL
    extra_query = ""
    if args.bank_senders_only:
        if not bank_senders:
            _emit({"error": "--bank-senders-only pero la cuenta no tiene bank_senders configurados"})
            return EXIT_BAD_ARGS
        extra_query = "(" + " OR ".join(f"from:{s}" for s in bank_senders) + ")"
    deps = deps_factory(args.account_id, access_token or "", extra_query=extra_query)
    message_day = (lambda mid, _t=access_token: go.fetch_message_day(_t, mid))
    del access_token

    try:
        if args.extract_headers:
            for line in go.extract_headers(gb.daterange(args.since, args.until), deps, only_new=args.only_new):
                _emit(line)
            return 0
        if args.hints:
            tools = deps.build_tool_map()
            for line in go.hints(tools, merchants):
                _emit(line)
            return 0
        if args.apply_decisions:
            created_ids: list[str] = []
            stats = go.apply_decisions(args.account_id, decisions, deps, dry_run=args.dry_run,
                                       created_ids=created_ids)
            linked = 0
            if created_ids and not args.dry_run:
                months = sorted({(d.year, d.month) for d in (go._valid_day(i.get("day")) for i in decisions
                                                             if isinstance(i, dict)) if d})
                try:
                    linked = gb.count_goal_linked(args.account_id, created_ids, months,
                                                  fetch_rows=deps.fetch_month_rows)
                except Exception:
                    linked = None
            _emit({"apply": {"dry_run": args.dry_run, **stats, "goal_linked_by_api": linked}})
            return 0
        # --extract-bodies (y/o --day-context, --categories)
        for line in go.extract_bodies(ids, deps, message_day, max_chars=args.max_chars):
            _emit(line)
        if args.day_context:
            _emit(go.day_context(args.account_id, args.day_context, deps))
        if args.categories:
            _emit(go.categories(deps.build_tool_map()))
        return 0
    except gb.httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        host = exc.request.url.host if exc.request is not None else "?"
        _emit({"error": f"HTTP {status} en {host}"})
        return EXIT_GMAIL


if __name__ == "__main__":
    sys.exit(main())
