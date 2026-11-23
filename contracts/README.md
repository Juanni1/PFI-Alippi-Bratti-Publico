# Contracts — Smart Contracts y Verificador zkML

Contratos inteligentes del prototipo de gestión de siniestros (Alippi y Bratti, UADE): registro de reclamos con máquina de siete estados y patrón `onlyOracle` (`ClaimRegistry.sol`), verificador de pruebas Halo2/KZG generado por EZKL (`Halo2Verifier.sol`), suite de pruebas y scripts de despliegue.

## Comandos

```shell
npm install          # instalar dependencias
npx hardhat test     # suite de pruebas sobre Hardhat Network local
npx hardhat coverage # reporte de cobertura (RNF-30)
npx hardhat node     # nodo JSON-RPC local en 127.0.0.1:8545
```

## Despliegue local

Con el nodo local levantado en otra terminal:

```shell
npx hardhat run scripts/deploy-local.js --network localhost
```

El script genera `despliegue-local.json` con las direcciones desplegadas y el ABI consumido por `backend/` mediante `CADENA_DESPLIEGUE`. Este archivo no se versiona porque el nodo local de Hardhat no persiste estado entre reinicios.

### Verificador zkML (`Halo2Verifier.sol`)

`contracts/verifier/Halo2Verifier.sol` es generado por EZKL a partir de la clave de verificación del circuito y no se edita manualmente. Se versiona para garantizar reproducibilidad del despliegue y se regenera desde `ml/` cuando cambia el modelo (`uv run python -m fase0.step7 --resolution 576 --out-dir artifacts_head`).

Es el único contrato compilado con optimizador activado (`runs=1`, igual que `ezkl.deploy_evm`), requisito necesario para que su bytecode entre dentro del límite EIP-170 sin recurrir a `allowUnlimitedContractSize`.

## Despliegue en Base Sepolia

La configuración de red y los límites de gas se definen en `config/base-sepolia.json`. El despliegue se ejecuta con `node scripts/deploy-testnet.js` (no mediante `hardhat run`).

Por defecto el script ejecuta únicamente **preflight** (sin firmar ni enviar transacciones): valida `chainId`, dirección del oráculo, nonce libre, ausencia de código en direcciones futuras, tamaño de bytecode EIP-170, estimación de gas, saldo disponible y cotizaciones L1/operador contra `GasPriceOracle`.

Variables de entorno requeridas (nunca guardar secretos en el repositorio ni pasarlos como argumentos CLI):

- `L2_RPC_URL`: endpoint RPC de Base Sepolia.
- `L2_PRIVATE_KEY`: clave privada de la cuenta del oráculo.
- `TESTNET_ORACLE_ADDRESS`: dirección pública esperada de esa clave.
- `TESTNET_OUTPUT`: ruta nueva donde escribir el artefacto JSON de despliegue (rechaza sobrescribir archivos existentes).
- `TESTNET_MAX_WEI`: presupuesto máximo estimado en wei antes de cada envío (cuatro límites de gas × `maxFeePerGas` más tarifas L1/operador estimadas; las tarifas efectivas pueden variar hasta la inclusión en bloque).

Para autorizar el envío real de transacciones se requiere además `TESTNET_SEND=SI_ENVIO_TRANSACCIONES`. Si un envío se interrumpe, el archivo de salida queda marcado con `estado: pendiente` junto con nonces, direcciones previstas y hashes deterministas registrados antes del broadcast para permitir verificación y recuperación sin reintentos ciegos. Solo un artefacto confirmado incluye el ABI y los hashes de bytecode consumidos por `backend/`.

## Redes

Por defecto las pruebas y el desarrollo corren sobre Hardhat Network local. La red remota Base Sepolia se activa únicamente al configurar `L2_RPC_URL` (ver `.env.example`).
