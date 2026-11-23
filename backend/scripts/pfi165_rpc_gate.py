"""Gate local de una corrida: reserva irreversible antes de transmitir bytes firmados."""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import fcntl
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes

READS = frozenset({"web3_clientVersion", "eth_chainId", "eth_getTransactionCount", "eth_getBalance", "eth_call",
    "eth_estimateGas", "eth_feeHistory", "eth_maxPriorityFeePerGas", "eth_gasPrice",
    "eth_getBlockByNumber", "eth_getCode", "eth_getTransactionReceipt",
    "eth_getTransactionByHash", "eth_blockNumber", "eth_getLogs"})
FUNCTIONS = frozenset({"registerClaim", "submitInference", "resolve", "settle"})
APPROVED_MAX_TRANSACTIONS = 16
APPROVED_MAX_WEI = 250_000_000_000_000


class Denied(Exception):
    """Rechazo público sin detalles de proveedor ni bytes firmados."""


def integer(value):
    if type(value) is not int or value < 0:
        raise Denied("Valor numérico inválido.")
    return value


def quantity(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise Denied("Respuesta RPC inválida.")
    try:
        return integer(int(value, 16))
    except ValueError:
        raise Denied("Respuesta RPC inválida.") from None


def address(value):
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise Denied("Identidad inválida.")
    try:
        bytes.fromhex(value[2:])
    except ValueError:
        raise Denied("Identidad inválida.") from None
    return value.lower()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Gate:
    def __init__(self, deployment, network_config, state, upstream, max_transactions,
                 max_wei, expected_start_nonce, lose_response_once_at):
        self.path = Path(state)
        if (self.path.is_symlink() or self.path.parent.is_symlink() or
                self.path.parent.stat().st_mode & 0o077 or
                (self.path.exists() and self.path.stat().st_mode & 0o077)):
            raise Denied("Ledger requiere directorio y archivo privados.")
        self.artifact = json.loads(Path(deployment).read_text())
        self.network = json.loads(Path(network_config).read_text())
        self.chain = integer(self.network["chainId"])
        if self.chain != self.artifact["red"]["chainId"]:
            raise Denied("Red incompatible.")
        self.wallet = address(self.artifact["oraculo"])
        self.registry = address(self.artifact["registro"])
        self.verifier = address(self.artifact["verificador"])
        self.oracle = address(self.network["gasPriceOracle"])
        self.start = integer(expected_start_nonce)
        self.limit = integer(max_transactions)
        self.budget = integer(max_wei)
        if (not 0 < self.limit <= APPROVED_MAX_TRANSACTIONS or
                not 0 < self.budget <= APPROVED_MAX_WEI or (lose_response_once_at is not None and
                (type(lose_response_once_at) is not int or not 1 <= lose_response_once_at <= self.limit))):
            raise Denied("Límites inválidos.")
        self.fault = lose_response_once_at
        self.binding = hashlib.sha256(canonical([self.artifact, self.network])).hexdigest()
        self.selectors = set()
        for item in self.artifact["abi"]:
            if item.get("type") == "function" and item.get("name") in FUNCTIONS:
                signature = item["name"] + "(" + ",".join(i["type"] for i in item["inputs"]) + ")"
                self.selectors.add(keccak(text=signature)[:4].hex())
        if len(self.selectors) != len(FUNCTIONS):
            raise Denied("ABI incompleto.")
        self.upstream = upstream
        if not upstream or not upstream.startswith(("http://", "https://")):
            raise Denied("RPC no configurada.")
        self.mutex = threading.Lock()
        self.poisoned = False
        # Un lock superviviente sin ledger es una corrida anterior incompleta, no un inicio nuevo.
        if Path(str(self.path) + ".lock").exists() and not self.path.exists():
            raise Denied("Ledger ausente; intervención necesaria.")
        # El lock separado permanece estable a través de os.replace; no se bloquea el inode del JSON.
        self.owner = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if os.fstat(self.owner).st_mode & 0o077:
            os.close(self.owner)
            raise Denied("Lock no privado.")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.ledger = json.loads(self.path.read_text())
                expected = self._empty()
                if any(self.ledger.get(k) != expected[k] for k in expected if k not in ("attempts", "reserved_wei", "actual_spent_wei", "fault_consumed")) or not isinstance(self.ledger.get("attempts"), list) or type(self.ledger.get("fault_consumed")) is not bool:
                    raise Denied("Ledger incompatible; intervención necesaria.")
                attempts = self.ledger["attempts"]
                if (type(self.ledger.get("reserved_wei")) is not int or
                        type(self.ledger.get("actual_spent_wei")) is not int or
                        self.ledger["reserved_wei"] != sum(integer(a["estimate_wei"]) for a in attempts) or
                        self.ledger["actual_spent_wei"] != sum(
                            integer(a["actual_wei"]) for a in attempts if a.get("actual_wei") is not None
                        ) or self.ledger["reserved_wei"] > self.budget or len(attempts) > self.limit):
                    raise Denied("Ledger corrupto; intervención necesaria.")
                for index, item in enumerate(attempts):
                    if (not isinstance(item, dict) or not isinstance(item.get("hash"), str) or
                            re.fullmatch(r"0x[0-9a-f]{64}", item["hash"]) is None or
                            item.get("nonce") != self.start + index or item.get("to") != self.registry or
                            item.get("selector") not in {"0x" + s for s in self.selectors} or
                            integer(item["estimate_wei"]) <= 0 or
                            "actual_wei" not in item or
                            (item["actual_wei"] is not None and integer(item["actual_wei"]) <= 0) or
                            item.get("status") not in ("uncertain", "confirmed") or
                            (item["status"] == "uncertain" and item["actual_wei"] is not None) or
                            (item["status"] == "confirmed" and type(item.get("block_number")) is not int) or
                            (index < len(attempts) - 1 and item["status"] != "confirmed")):
                        raise Denied("Ledger corrupto; intervención necesaria.")
            else:
                self.ledger = self._empty()
                self._persist()
            self._verify_chain()
            for attempt in self.ledger["attempts"]:
                if attempt["status"] == "confirmed" and not self._receipt(attempt):
                    raise Denied("Recibo confirmado ausente; intervención necesaria.")
            confirmed = sum(a["status"] == "confirmed" for a in self.ledger["attempts"])
            if self.ledger["attempts"] and self.ledger["attempts"][-1]["status"] != "confirmed":
                return
            if self._nonce("latest") != self.start + confirmed or self._nonce("pending") != self.start + confirmed:
                raise Denied("Nonce externo incompatible; intervención necesaria.")
        except Exception:
            os.close(self.owner)
            raise

    def _empty(self):
        return {"version": 2, "binding": self.binding, "chain_id": self.chain,
                "wallet": self.wallet, "registry": self.registry, "fee_oracle": self.oracle,
                "start_nonce": self.start, "max_transactions": self.limit, "max_wei": self.budget,
                "fault_attempt": self.fault, "fault_consumed": False, "reserved_wei": 0,
                "actual_spent_wei": 0, "attempts": []}

    def _persist(self):
        # Un archivo nuevo y fsync antes del reemplazo; jamás truncar el registro anterior.
        import tempfile
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".pending-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(self.ledger) + b"\n")
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            # Un resto pending se conserva para diagnóstico; nunca sustituir un ledger sano.
            self.poisoned = True
            raise Denied("Persistencia fallida; detener la corrida.") from None

    def _rpc(self, method, params):
        try:
            body = canonical({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            req = Request(self.upstream, body, {"Content-Type": "application/json"})
            with urlopen(req, timeout=15) as response:
                data = json.loads(response.read(2_000_000))
            if not isinstance(data, dict) or data.get("id") != 1 or "result" not in data or "error" in data:
                raise ValueError()
            return data["result"]
        except Exception:
            raise Denied("RPC no disponible; intervención necesaria.") from None

    def _nonce(self, tag):
        return quantity(self._rpc("eth_getTransactionCount", [self.wallet, tag]))

    def _verify_chain(self):
        if quantity(self._rpc("eth_chainId", [])) != self.chain:
            raise Denied("Red incompatible.")
        for key, target in (("registroHash", self.registry), ("verificadorHash", self.verifier)):
            code = self._rpc("eth_getCode", [target, "latest"])
            try:
                digest = "0x" + keccak(HexBytes(code)).hex()
            except Exception:
                raise Denied("Código incompatible.") from None
            if digest.lower() != self.artifact["codigo"][key].lower():
                raise Denied("Código incompatible.")
        for signature, expected in (("oracle()", self.wallet), ("verifier()", self.verifier)):
            answer = self._rpc("eth_call", [{"to": self.registry, "data": "0x" + keccak(text=signature)[:4].hex()}, "latest"])
            if not isinstance(answer, str) or len(answer) != 66 or "0x" + answer[-40:].lower() != expected:
                raise Denied("Contrato incompatible.")
        code = self._rpc("eth_getCode", [self.oracle, "latest"])
        if not isinstance(code, str) or code in ("0x", "0x0"):
            raise Denied("Oráculo de tarifas ausente.")

    def _actual_fee(self, receipt, block):
        """Acepta solo una medición completa; no convierte ausencias en cero."""
        try:
            gas = quantity(receipt.get("gasUsed"))
            price = quantity(receipt.get("effectiveGasPrice"))
            l1 = quantity(receipt.get("l1Fee"))
            operator = receipt.get("operatorFee")
            if operator is None:
                data = "0x" + keccak(text="getOperatorFee(uint256)")[:4].hex() + gas.to_bytes(32, "big").hex()
                operator = self._rpc("eth_call", [{"to": self.oracle, "data": data}, hex(block)])
            operator = quantity(operator)
            if gas <= 0 or price <= 0:
                return None
            return gas * price + l1 + operator
        except (Denied, OverflowError):
            return None

    def _receipt(self, attempt):
        receipt = self._rpc("eth_getTransactionReceipt", [attempt["hash"]])
        if receipt is None:
            return False
        tx = self._rpc("eth_getTransactionByHash", [attempt["hash"]])
        if not isinstance(receipt, dict) or not isinstance(tx, dict) or any((
                str(tx.get("hash", "")).lower() != attempt["hash"],
                str(receipt.get("transactionHash", "")).lower() != attempt["hash"],
                str(tx.get("from", "")).lower() != self.wallet,
                str(tx.get("to", "")).lower() != attempt["to"],
                str(receipt.get("from", "")).lower() != self.wallet,
                str(receipt.get("to", "")).lower() != attempt["to"],
                str(tx.get("input", "")).lower()[:10] != attempt["selector"],
                quantity(tx.get("nonce")) != attempt["nonce"],
                quantity(tx.get("chainId")) != self.chain,
                quantity(tx.get("value")) != 0,
                quantity(receipt.get("status")) != 1,
                receipt.get("blockNumber") is None)):
            raise Denied("Recibo incompatible; intervención necesaria.")
        block = quantity(receipt["blockNumber"])
        actual = self._actual_fee(receipt, block)
        if attempt["status"] == "confirmed":
            if (attempt["block_number"] != block or
                    (attempt["actual_wei"] is not None and attempt["actual_wei"] != actual)):
                raise Denied("Recibo cambiado; intervención necesaria.")
            if attempt["actual_wei"] is not None or actual is None:
                return True
        attempt["status"] = "confirmed"
        attempt["block_number"] = block
        attempt["actual_wei"] = actual
        if actual is not None:
            self.ledger["actual_spent_wei"] += actual
        self._persist()
        return True

    def _quote(self, gas, fee, size):
        total = gas * fee
        for signature, argument in (("getL1FeeUpperBound(uint256)", size), ("getOperatorFee(uint256)", gas)):
            data = "0x" + keccak(text=signature)[:4].hex() + argument.to_bytes(32, "big").hex()
            total += quantity(self._rpc("eth_call", [{"to": self.oracle, "data": data}, "latest"]))
        return total

    def _send(self, params):
        self._check_health()
        if not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], str):
            raise Denied("Transacción inválida.")
        raw = params[0]
        try:
            encoded = HexBytes(raw)
            if encoded[0] != 2:
                raise ValueError()
            tx = TypedTransaction.from_bytes(encoded).as_dict()
            sender = Account.recover_transaction(raw).lower()
            digest = "0x" + keccak(encoded).hex()
            to = "0x" + tx["to"].hex()
            selector = tx["data"][:4].hex()
            gas, fee, priority = (integer(tx[k]) for k in ("gas", "maxFeePerGas", "maxPriorityFeePerGas"))
            nonce = integer(tx["nonce"])
            if (integer(tx["chainId"]) != self.chain or sender != self.wallet or
                    to.lower() != self.registry or selector not in self.selectors or
                    integer(tx["value"]) != 0 or not gas or not fee or priority > fee or
                    len(tx["data"]) < 4):
                raise ValueError()
        except Exception:
            raise Denied("Transacción no autorizada.") from None
        attempts = self.ledger["attempts"]
        if attempts and attempts[-1]["status"] != "confirmed":
            last = attempts[-1]
            if digest == last["hash"] and self._receipt(last):
                return digest
            raise Denied("Transacción incierta; intervención necesaria.")
        if any(a["hash"] == digest or a["nonce"] == nonce for a in attempts):
            raise Denied("Transacción ya reservada.")
        if len(attempts) >= self.limit or nonce != self.start + len(attempts):
            raise Denied("Límite o nonce no autorizado.")
        if any(a["actual_wei"] is None for a in attempts) or self.ledger["actual_spent_wei"] > self.budget:
            raise Denied("Gasto real desconocido o superior al presupuesto; intervención necesaria.")
        self._verify_chain()
        if self._nonce("latest") != nonce or self._nonce("pending") != nonce:
            raise Denied("Nonce externo incompatible; intervención necesaria.")
        estimate = self._quote(gas, fee, len(encoded))
        if (self.ledger["reserved_wei"] + estimate > self.budget or
                self.ledger["actual_spent_wei"] + estimate > self.budget or
                quantity(self._rpc("eth_getBalance", [self.wallet, "latest"])) < estimate):
            raise Denied("Saldo o presupuesto insuficiente.")
        attempt = {"hash": digest, "nonce": nonce, "to": to.lower(), "selector": "0x" + selector,
                   "estimate_wei": estimate, "actual_wei": None, "status": "uncertain"}
        # Nunca se envía si la reserva completa no está durable.
        previous = self.ledger
        self.ledger = {**previous, "reserved_wei": previous["reserved_wei"] + estimate,
                       "attempts": attempts + [attempt]}
        try:
            self._persist()
        except Denied:
            self.ledger = previous
            raise
        try:
            answer = self._rpc("eth_sendRawTransaction", [raw])
            if not isinstance(answer, str) or answer.lower() != digest:
                raise Denied("Respuesta de envío incierta; intervención necesaria.")
            if self.fault == len(self.ledger["attempts"]) and not self.ledger["fault_consumed"]:
                self.ledger["fault_consumed"] = True
                self._persist()
                raise Denied("Respuesta controlada perdida; intervención necesaria.")
            return digest
        except Denied:
            raise

    def _check_health(self):
        if self.poisoned:
            raise Denied("Persistencia incierta; detener la corrida.")

    def handle(self, method, params):
        self._check_health()
        if method in READS:
            if not isinstance(params, list):
                raise Denied("Parámetros inválidos.")
            # La lectura del recibo puede reconciliar únicamente la identidad reservada.
            if method == "eth_getTransactionReceipt" and len(params) == 1:
                with self.mutex:
                    self._check_health()
                    for attempt in self.ledger["attempts"]:
                        if params[0] == attempt["hash"] and attempt["status"] != "confirmed":
                            self._receipt(attempt)
            return self._rpc(method, params)
        if method == "eth_sendRawTransaction":
            with self.mutex:
                self._check_health()
                return self._send(params)
        raise Denied("Método RPC no autorizado.")


class LocalServer(ThreadingHTTPServer):
    def __init__(self, bind, gate):
        self.gate = gate
        super().__init__(bind, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request_id = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise Denied("Solicitud inválida.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or type(payload.get("id")) not in (str, int) or not isinstance(payload.get("method"), str):
                raise Denied("Solicitud inválida.")
            request_id = payload["id"]
            answer = {"jsonrpc": "2.0", "id": request_id}
            answer["result"] = self.server.gate.handle(payload["method"], payload.get("params", []))
        except Exception:
            answer = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "Solicitud rechazada; consulte el ledger."}}
        content = canonical(answer)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    parser = argparse.ArgumentParser(description="Gate JSON-RPC local con reserva durable previa al envío.")
    for flag in ("deployment", "network-config", "state", "listen"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--max-transactions", type=int, required=True)
    parser.add_argument("--max-wei", type=int, required=True)
    parser.add_argument("--expected-start-nonce", type=int, required=True)
    parser.add_argument("--lose-response-once-at", type=int, help="Falla controlada única tras broadcast confirmado por RPC")
    args = parser.parse_args()
    try:
        host, port = args.listen.rsplit(":", 1)
        if host != "127.0.0.1" or not 0 <= int(port) <= 65535:
            raise Denied("Solo se permite 127.0.0.1.")
        gate = Gate(args.deployment, args.network_config, args.state, os.environ.get("PFI165_UPSTREAM_RPC"),
                    args.max_transactions, args.max_wei, args.expected_start_nonce, args.lose_response_once_at)
        with LocalServer((host, int(port)), gate) as server:
            print(json.dumps({"listen": f"127.0.0.1:{server.server_port}",
                              "transactions": len(gate.ledger["attempts"]),
                              "reserved_wei": gate.ledger["reserved_wei"]}), flush=True)
            server.serve_forever()
    except Exception:
        print("Error: gate no disponible; verifique configuración, ledger y RPC.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
