"""
Модуль автоматизации и аналитики отчетов ЭТРН (Электронные транспортные накладные).
"""

try:
    from .config import Config, get_config
    from .etrn_optimizer import process_and_style_etrn
    from .etrn_service import EtrnService
    from .onec_exporter import BaseOneCExporter, get_onec_exporter
except ImportError:
    from config import Config, get_config
    from etrn_optimizer import process_and_style_etrn
    from etrn_service import EtrnService
    from onec_exporter import BaseOneCExporter, get_onec_exporter

__all__ = [
    "Config",
    "get_config",
    "EtrnService",
    "process_and_style_etrn",
    "BaseOneCExporter",
    "get_onec_exporter",
]
