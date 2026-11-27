"""Public diagnostics for tool and job callers."""
from __future__ import annotations

from enum import StrEnum
import sqlite3

from pydantic import ValidationError


class ErrorCategory(StrEnum):
    STALE_CONFLICT = 'STALE_CONFLICT'
    GEODETIC_CONTRACT = 'GEODETIC_CONTRACT'
    DOMAIN_PRECONDITION = 'DOMAIN_PRECONDITION'
    VALIDATION_ERROR = 'VALIDATION_ERROR'
    AUTHORIZATION_BOUNDARY = 'AUTHORIZATION_BOUNDARY'
    NATIVE_RUNTIME = 'NATIVE_RUNTIME'
    ARTIFACT_STATE = 'ARTIFACT_STATE'
    PUBLISHER_FENCING = 'PUBLISHER_FENCING'
    NOT_FOUND = 'NOT_FOUND'
    INTERNAL_ERROR = 'INTERNAL_ERROR'


_FAMILY = {
    ErrorCategory.STALE_CONFLICT: ('The request no longer matches its bound inputs or state.', 'mapgis_tx_manage',
                                   'status', 'Inspect the request and bind fresh input hashes to a new preview.'),
    ErrorCategory.GEODETIC_CONTRACT: ('The coordinate frames or units do not satisfy the operation.', 'mapgis_audit_check',
                                       None, 'Verify the datum, axis order, units and false easting before combining inputs.'),
    ErrorCategory.DOMAIN_PRECONDITION: ('The operation requires an additional domain precondition.', 'mapgis_discover',
                                        'describe', 'Review the operation preconditions and required local evidence.'),
    ErrorCategory.VALIDATION_ERROR: ('The tool arguments or input format failed validation.', 'mapgis_discover',
                                     'describe', 'Use the registered input schema and check the supplied fields.'),
    ErrorCategory.AUTHORIZATION_BOUNDARY: ('The requested action is outside the configured access boundary.',
                                           'mapgis_environment', None, 'Use a project-owned work copy and an authorized route.'),
    ErrorCategory.NATIVE_RUNTIME: ('The native runtime could not complete or confirm the request.', 'mapgis_tx_recovery',
                                   'native', 'Read the existing request status and native evidence before retrying.'),
    ErrorCategory.ARTIFACT_STATE: ('The saved artifact or request is in an incompatible state.', 'mapgis_tx_manage',
                                   'status', 'Inspect the request and reconcile completed evidence before any further action.'),
    ErrorCategory.PUBLISHER_FENCING: ('The publication epoch is stale or missing.', 'mapgis_tx_manage',
                                       'current_epoch', 'Check current ownership and use the active publisher epoch.'),
    ErrorCategory.NOT_FOUND: ('No matching file, operation, tool or request was found.', 'mapgis_discover',
                               'find', 'Check the identifier or inspect the available operations.'),
    ErrorCategory.INTERNAL_ERROR: ('The tool could not complete the operation.', None,
                                    None, 'Record the operation and request ID and inspect the local execution evidence.'),
}

# Public codes are fixed. Exception bodies can contain paths, SQL, Pydantic
# input values or process output and are deliberately excluded from the envelope.
_PUBLIC_CODES = {
    ErrorCategory.STALE_CONFLICT: ('STALE_SOURCE', 'STALE_SOURCE_MEMBERS', 'REQUEST_ID_CONFLICT',
                                   'FIELD_CONFLICT', 'STRUCTURAL_CONFLICT', 'JOB_REQUEST_CONFLICT', 'CRS_SIDECAR_STALE'),
    ErrorCategory.GEODETIC_CONTRACT: ('AUDIT_CRS_MISMATCH', 'AUDIT_FALSE_EASTING', 'AUDIT_UNPROJECTED_MIX',
                                      'EARTH_CRS_REQUIRED', 'GEOGRAPHIC_CRS_REQUIRED', 'SPATIAL_CRS_MISMATCH',
                                      'PROJECTED_UNITS_REQUIRED', 'EXPLICIT_CRS', 'EXPLICIT_LOCAL_FRAME',
                                      'MISSING_BEST_GRID', 'PROJECTION_DOMAIN', 'SHEET_GEOGRAPHIC_CRS',
                                      'CRS_SIDECAR_INVALID', 'CRS_SIDECAR_NATIVE_HEADER', 'DBS_CRS',
                                      'DBS_FALSE_EASTING', 'SURVEY_WT_CRS_CONTRACT', 'SURVEY_WT_SCALE_REQUIRED',
                                      'INTERCHANGE_CRS'),
    ErrorCategory.DOMAIN_PRECONDITION: ('HOST_OBSERVATION_REQUIRED', 'INVALID_GEOMETRY',
                                        'EXPLICIT_LINE_ROLE_REQUIRED', 'GEOREFERENCE_NOT_ACCEPTED',
                                        'DBS_ADJACENCY', 'DBS_TOPOLOGY', 'SURVEY_WT_LAYOUT',
                                        'INTERCHANGE_GEOMETRY', 'PROVIDER_UNAVAILABLE'),
    ErrorCategory.VALIDATION_ERROR: ('INVALID_SURFACE', 'INVALID_LIMIT', 'INVALID_JOB_PROGRESS',
                                     'UNEXPECTED_PARAMETERS', 'INVALID_TOOL_ARGUMENTS', 'PLAN_CYCLE',
                                     'PLAN_DUPLICATE_NODE', 'PLAN_UNKNOWN_NODE', 'PLAN_OUTPUT_KEY',
                                     'PLAN_EXCLUSIVE_SESSION', 'PLAN_FORMAT', 'PLAN_PARAMETERS', 'PLAN_INPUTS',
                                     'FDR_MODE', 'FDR_REDACTION', 'PREVIEW_DIFF_SIZE', 'DBS_SCHEMA', 'DBS_FIELD_GBK',
                                     'DBS_BOUNDARY', 'DBS_TOLERANCE', 'DBS_WINDOW', 'DBS_POLICY', 'L2_PARAMETER_BLOCK',
                                     'SURVEY_WT_FORMAT', 'INTERCHANGE_TARGET', 'INTERCHANGE_SOURCE',
                                     'INTERCHANGE_LAYER', 'INTERCHANGE_STYLE', 'INTERCHANGE_METADATA',
                                     'INTERCHANGE_SURPAC', 'INTERCHANGE_DRILLHOLES'),
    ErrorCategory.AUTHORIZATION_BOUNDARY: ('PRIVATE_UI_AUTHORIZATION_REQUIRED', 'ARTIFACT_OBJECT_PATH',
                                           'PLAN_UNBOUND_READ', 'PLAN_OUTPUT_PATH', 'PLAN_EFFECT', 'FDR_LEAK',
                                           'DBS_WRITE_ROOT_REQUIRED', 'INTERCHANGE_WRITE_ROOT'),
    ErrorCategory.NATIVE_RUNTIME: ('NATIVE_PROTOCOL', 'NATIVE_RESULT_DIFF', 'NATIVE_TIMEOUT', 'NATIVE_RUNTIME',
                                   'NATIVE_L2_BUILD_MISMATCH', 'NATIVE_L2_READBACK', 'SURVEY_WT_READBACK'),
    ErrorCategory.ARTIFACT_STATE: ('OUTPUT_CONFLICT', 'OUTPUT_MISSING', 'INCOMPLETE_OUTPUT', 'ARTIFACT_INCOMPLETE',
                                   'ARTIFACT_PRODUCER_RUNNING', 'ARTIFACT_PUBLICATION_CONFLICT',
                                   'ARTIFACT_CANDIDATE_CHANGED', 'ARTIFACT_OBJECT_MISSING', 'ARTIFACT_OBJECT_CORRUPT',
                                   'JOB_UNCERTAIN', 'JOB_INTERRUPTED', 'JOB_CANCELLED',
                                   'PLAN_BUSY', 'PLAN_CHECKPOINT_CORRUPT', 'PLAN_OUTPUT_CHANGED', 'FDR_INTEGRITY',
                                   'DBS_INDEX', 'DBS_READBACK', 'INTERCHANGE_INTEGRITY', 'INTERCHANGE_READBACK'),
    ErrorCategory.PUBLISHER_FENCING: ('STALE_PUBLISHER_EPOCH', 'PUBLISHER_EPOCH_REQUIRED'),
    ErrorCategory.NOT_FOUND: ('UNKNOWN_OPERATION', 'UNKNOWN_TOOL', 'UNKNOWN_REQUEST', 'UNKNOWN_JOB',
                              'PLAN_MISSING', 'FDR_REQUEST_MISSING', 'UNKNOWN_PROVIDER'),
}
_CODE_FAMILY = {code: category for category, codes in _PUBLIC_CODES.items() for code in codes}


def classify_error(exc: Exception) -> dict:
    """Return a serializable envelope without exposing the raw exception or cause."""
    text = str(exc)
    prefix = text.split(':', 1)[0].splitlines()
    code = prefix[0].strip() if prefix else ''
    category = _CODE_FAMILY.get(code)
    retryable = False
    if isinstance(exc, ValidationError):
        category, code = ErrorCategory.VALIDATION_ERROR, 'INVALID_TOOL_ARGUMENTS'
    elif category is None:
        if isinstance(exc, FileNotFoundError) or text.startswith(('Unknown request_id', 'Unknown tool')):
            category = ErrorCategory.NOT_FOUND
            code = 'UNKNOWN_REQUEST' if text.startswith('Unknown request_id') else category.value
        elif isinstance(exc, PermissionError) or text.startswith(('Writes require ', 'PRIVATE_UI_NOT_AUTHORIZED')):
            category = ErrorCategory.AUTHORIZATION_BOUNDARY
        elif code.startswith('STALE_') or code.endswith('_CONFLICT'):
            category = ErrorCategory.STALE_CONFLICT
        elif (any(part in code for part in ('_CRS_', '_FRAME_MISMATCH', 'FALSE_EASTING'))
              or code.startswith('CRS_') or code.endswith('_CRS')):
            category = ErrorCategory.GEODETIC_CONTRACT
        elif code.startswith(('ARTIFACT_', 'OUTPUT_', 'JOB_')) or text.startswith(
                ('This request was cancelled', 'An applied or uncertain request', 'Request is not prepared')):
            category = ErrorCategory.ARTIFACT_STATE
        elif isinstance(exc, TimeoutError) or code.startswith(('NATIVE_', 'PRIVATE_UI_')):
            category = ErrorCategory.NATIVE_RUNTIME
        elif isinstance(exc, sqlite3.OperationalError) and 'locked' in text.casefold():
            category, code, retryable = ErrorCategory.ARTIFACT_STATE, 'LEDGER_BUSY', True
        elif isinstance(exc, ValueError):
            category = ErrorCategory.VALIDATION_ERROR
        else:
            category = ErrorCategory.INTERNAL_ERROR
        if code not in {'UNKNOWN_REQUEST', 'LEDGER_BUSY'}:
            code = category.value
    message, tool, operation, hint = _FAMILY[category]
    if code == 'LEDGER_BUSY':
        message = 'The local ledger is busy.'
        hint = 'Reconcile the same request ID before retrying.'
    elif code in {'ARTIFACT_INCOMPLETE', 'JOB_UNCERTAIN', 'JOB_INTERRUPTED'}:
        hint = 'Reconcile the existing operation evidence before submitting new work.'
    elif code in {'PLAN_BUSY', 'PLAN_CHECKPOINT_CORRUPT'}:
        tool, operation = 'mapgis_run_plan', 'resume'
        hint = 'Check the current plan and its CAS checkpoint before resuming.'
        retryable = code == 'PLAN_BUSY'
    return {'error_code': code, 'category': category.value, 'retryable': retryable, 'message': message,
            'recovery': {'suggested_tool': tool, 'suggested_operation': operation, 'hint': hint}}
