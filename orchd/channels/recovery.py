"""Explicit recovery after fencing/stopping the previous outbound runner.

An abandoned claim is evidence of an unknown external effect. It never grants a
new send, even when a later investigation proves that the provider rejected it.
"""
from . import repository


def cutover(service, source_id, mode, *, backfill=None):
    return repository.cutover(service.con, source_id, mode, service.clock(), backfill)


def recover(service):
    return repository.recover_outbound(service.con, service.clock())


def reconcile(service, delivery_id, outcome, receipt, evidence):
    return repository.reconcile_outbound(service.con, delivery_id, outcome, receipt, evidence, service.clock())
