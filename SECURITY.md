# Security policy

Please report vulnerabilities through the repository's private security reporting
channel or contact the project maintainer privately. Include the affected version
or surface, the relevant operation ID, the impact and a minimal reproduction
using disposable data. Remove credentials, private source data, workstation logs,
MapGIS installation files and licence material from the report.

This connector runs over local MCP stdio. Its configured task root owns work
copies, artifacts and request evidence. Administer that root as private project
data and give access only to the intended client and project users.

Native operations use the supported build and explicit local opt-ins. Private
desktop routes bind owned processes and task copies. Client-facing tool hints
describe side effects. The server enforces ownership, hashes, revisions and
publication fences in the operation paths that accept them.

If a publishing or native operation is interrupted, retain its request ID and
ledger, check the recorded state, and reconcile before creating a new request.
