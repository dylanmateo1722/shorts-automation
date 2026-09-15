"""shorts-automation — orquestador del pipeline de producción de Shorts.

Gate 1 implementa únicamente la columna vertebral técnica: contratos,
directorio de ejecución, manifest, idempotencia, adaptador de
MoneyPrinterTurbo y un pipeline mínimo. Las etapas lingüísticas y de
publicación llegan en Gates posteriores.
"""

__version__ = "0.1.0"
