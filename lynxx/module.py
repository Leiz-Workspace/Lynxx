# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
Lynxx: module ids are now a dynamic registry so site downloaders can be shipped as extensions
"""
#########################################
#
#

from typing import final

from .defines import (
    MODULE_ABBR_BB,
    MODULE_ABBR_DB,
    MODULE_ABBR_EN,
    MODULE_ABBR_RN,
    MODULE_ABBR_RP,
    MODULE_ABBR_RS,
    MODULE_ABBR_RX,
    MODULE_ABBR_XB,
)

__all__ = ('ProcModule',)

# ids of modules known to the core are stable (they are stored in user configs)
# extensions not known to the core receive ids starting from this value
EXTERNAL_MODULE_ID_BASE = 100


@final
class ProcModule:
    RX: int = 1
    RN: int = 2
    RS: int = 3
    RP: int = 4
    EN: int = 5
    XB: int = 6
    BB: int = 7
    DB: int = 8

    PROC_MODULES_BY_NAME: dict[str, int] = {
        MODULE_ABBR_RX: RX,
        MODULE_ABBR_RN: RN,
        MODULE_ABBR_RS: RS,
        MODULE_ABBR_RP: RP,
        MODULE_ABBR_EN: EN,
        MODULE_ABBR_XB: XB,
        MODULE_ABBR_BB: BB,
        MODULE_ABBR_DB: DB,
    }
    PROC_MODULES_NAMES_BY_ID: dict[int, str] = {v: k for k, v in PROC_MODULES_BY_NAME.items()}

    PROC_MODULE_DEFAULT: int = RX
    PROC_MODULE_NAME_DEFAULT: str = PROC_MODULES_NAMES_BY_ID[PROC_MODULE_DEFAULT]
    CUR_PROC_MODULE: int = PROC_MODULE_DEFAULT
    PROC_MODULE_MIN: int = RX
    PROC_MODULE_MAX: int = DB
    '''max id among the modules known to the core'''

    _next_external_id: int = EXTERNAL_MODULE_ID_BASE

    @staticmethod
    def register(abbr: str) -> int:
        """Returns a stable id for a known module abbreviation or allocates a new one for an external extension"""
        abbr = abbr.lower()
        if abbr in ProcModule.PROC_MODULES_BY_NAME:
            return ProcModule.PROC_MODULES_BY_NAME[abbr]
        new_id = ProcModule._next_external_id
        ProcModule._next_external_id += 1
        ProcModule.PROC_MODULES_BY_NAME[abbr] = new_id
        ProcModule.PROC_MODULES_NAMES_BY_ID[new_id] = abbr
        return new_id

    @staticmethod
    def known_names() -> tuple[str, ...]:
        return tuple(ProcModule.PROC_MODULES_BY_NAME.keys())

    @staticmethod
    def set(dwnmodule: int) -> None:
        ProcModule.CUR_PROC_MODULE = dwnmodule

    @staticmethod
    def value() -> int:
        return ProcModule.CUR_PROC_MODULE

    @staticmethod
    def name() -> str:
        return ProcModule.PROC_MODULES_NAMES_BY_ID.get(ProcModule.value(), 'unk')

    @staticmethod
    def set_cur_module_by_name(name: str) -> None:
        ProcModule.set(ProcModule.PROC_MODULES_BY_NAME.get(name, ProcModule.RX))

    @staticmethod
    def is_rx() -> bool:
        return ProcModule.value() == ProcModule.RX

    @staticmethod
    def is_rn() -> bool:
        return ProcModule.value() == ProcModule.RN

    @staticmethod
    def is_rs() -> bool:
        return ProcModule.value() == ProcModule.RS

    @staticmethod
    def is_rp() -> bool:
        return ProcModule.value() == ProcModule.RP

    @staticmethod
    def is_en() -> bool:
        return ProcModule.value() == ProcModule.EN

    @staticmethod
    def is_xb() -> bool:
        return ProcModule.value() == ProcModule.XB

    @staticmethod
    def is_bb() -> bool:
        return ProcModule.value() == ProcModule.BB

    @staticmethod
    def is_db() -> bool:
        return ProcModule.value() == ProcModule.DB

#
#
#########################################
