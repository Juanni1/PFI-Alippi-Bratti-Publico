"""Capa de adaptadores del DCOMP: repositorio relacional (SQLAlchemy /
PostgreSQL con migraciones Alembic y UTC), cliente IPFS (Kubo por API HTTP; las
conexiones se abren por job y se cierran al terminar, sin sondeos periódicos),
motor de inferencia (ONNX Runtime, extractor + cabeza), generador
de pruebas (EZKL) y cliente on-chain (web3.py, oráculo).

Escritos: `models` (las nueve entidades del DER con marcas temporales UTC),
`database` (PostgreSQL con verificación de esquema y ciclo de vida de sesión),
`ipfs_client`, `inference`, `proving` y `chain`.

De los tres últimos, la inferencia tiene sus dos implementaciones y las otras
dos entran simuladas: la real de una es EZKL y la de la otra web3.py contra
Hardhat, y las dos fallan de forma explícita si se las pide por entorno, para
que nadie caiga en silencio al simulador creyendo que hay criptografía o
cadena detrás."""
