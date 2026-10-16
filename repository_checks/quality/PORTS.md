# Source assertion crosswalk

The initial copied cohort comprised 129 test methods in 21 test modules; these
are historical import counts, not the current directory inventory. Each copied
module links its exact source revision. At import, detector fixtures retained
their original names and assertions except the local adaptations identified
below. No failing production source was changed, and no skip, xfail, local waiver
or new default allowance was introduced as an import workaround.

The source checkouts were inspected at these clean check-path revisions:

| Source | Commit |
| --- | --- |
| nmr-api-v1 | `dbd81a81ee92bd04d3223f4389a26891181cc0cd` |
| dapp32 | `778c16f8d8b77181d2ac4cd11a62a2034ded2518` |
| vote-mcp | `6f595850d199e0d97098c1a7e67ddff774171a25` |

The common source root is the launcher's pinned `/workspace`. Authored Python
roots are tools, tests, repository_checks, and the optional future
nmrnet_inference package. The upstream NMRNetplusplus snapshot is separately
owned. Cache and image discovery additionally govern containers. Production-only
API scans map to tools and nmrnet_inference; API test-only scans map to both local
test suites. This changes inventories, not detector predicates.

Later reviewed Python contracts use the local mechanism in
[test_python.py](silent_defaults/test_python.py), with tests exercising the property in
[test_python_contracts.py](silent_defaults/test_python_contracts.py). Annotations
leave raw finding predicates and the scan inventory unchanged. A standalone annotation
names a reason and an existing local test target. Its scope is one generic
expression on a single-line statement or a single-line condition expression in
an `If` header or a `For` iterable, one outermost generic expression and its generic children in
`With` context expressions or
`Assign`/`Return` values (including multiline values), the whole value of a
Call-valued `Expr`, or callable signature defaults.
Compound bodies, `For`/`With`/`Assign` targets, and callable decorators, type annotations
and bodies remain outside those boundaries.
The `For` placement covers only a single-line iterable, including when its body
starts on that line. Its target, body and `else` suite remain visible; this
dedicated header placement supports neither multiline iterables nor `AsyncFor`.
The existing single-line statement fallback is unchanged. The dedicated `For`
placement uses the same generic-only, operator-input, one-outermost-expression
and no-lambda guards.
An annotation before a `Return` covers its value only; sibling expressions,
adjacent statements and any enclosing exception-fallback finding remain visible.
A Call-valued `Expr` covers the callee/receiver and arguments of that one call,
including multiline calls, under the same outermost-expression, generic-only,
operator-input and no-lambda guards. This supports reviewed consumer invocations,
not inferred call purity or assertion semantics. Other multiline `Expr` values,
including bare containers, remain unsupported. Existing single-line statement
support is unchanged; this narrow syntax extension does not promise formatting
invariance for every expression statement. The discarded ENV-pop tag retains
its separate, earlier exact-call boundary.

A dedicated annotation immediately before a literal-key `Dict` entry covers
only that entry's value, including multiline values, under the same one-outermost
generic-expression and no-lambda guards. All keys in that `Dict` must be literal
constants; any computed key or unpack entry rejects this placement. Exactly one
key AST must start on the next line at the comment's indentation. Multiple key
ASTs starting on that line and sibling defaults within the value do not qualify.
This is an AST key boundary, not a parser for entry punctuation; parentheses can
move a separate literal key's AST start onto a later line. That separate entry
remains outside the annotated value.
Key expressions, sibling entries and enclosing defaults remain independently
visible. ENV and CLI inputs retain the ordinary strict guards. This permits
reviewed observation sentinels without treating the containing dictionary as
one contract; it does not infer that metadata defaults are safe.

A separate detector correction classifies OR-combined comparison predicates as
validation results rather than replacement inputs. Mixed value fallbacks and
nested defaults, including ENV `get` defaults and `env["PORT"] or 443`, remain
findings in their own right. This syntactic distinction does not resolve aliases
or overloaded comparisons, prove call purity, or establish that validation is
correct or fails closed.

A separate applicability correction retains raw OR findings but admits an OR
consumed only as an `If` branch truth through `BoolOp`/`Not` edges when every
operand has a recognized ENV read, an ENV-containing comparison, or such evidence
through further boolean edges. Opaque calls and other value consumers do not
provide that evidence. This covers independent optional fault activations;
assignments, returns, call arguments and ORs consumed by comparisons remain
strict. Nested defaults and body findings remain independently applicable.

The existing `allow-pop-default` tag uses the same reason, proof and placement
checks for one discarded ENV `pop` call with a literal string key, literal
`None`, exactly two positional arguments and no keywords. Only that call's
finding is admitted; consumed values, other defaults and adjacent findings stay
strict. This declares idempotent revocation, not a replacement input or outcome.

Recognized ENV reads, raw-argument, argparse and args-attribute rules remain strict.
A handler in an ordinary `try` may be reviewed when it contains only `pass`,
declares specific exception names or dotted names (optionally a flat tuple), and
neither its header nor protected suite has recognized ENV/CLI access. Only that
handler's finding is admitted. Bare, broad or computed types, `except*` and return
fallbacks remain outside this boundary. Static test reference validation catches missing or
renamed proof symbols without executing them; it does not resolve exception
aliases or certify a contract's meaning or proof coverage. Human review owns
those decisions and their limits. Subsequent fixes, explicit fixture
adaptations and qualified applicability decisions are recorded in
[note 011](../../notes/011_repository_check_ports_20261014.txt).

## Complete copied Python and operator detector modules

| Original source path | Local path | Methods | Original assertion groups retained |
| --- | --- | ---: | --- |
| API `repository_checks/test_validation_guard_posture.py` | same basename | 5 | Direct rejected orderings, split accepted ranges, mixed rejected terms, inverse optional predicate; exact positive/negative fixture shapes and check-self scan |
| API `repository_checks/test_tuple_generator_posture.py` | same basename | 3 | Generator/list comprehension to tuple, async/nested forms, adjacent concrete reason and second checker tag, invalid tag placement; whole authored Python scan |
| API `repository_checks/test_assert_equal_argument_order_posture.py` | same basename | 4 | Visible literal expectation first, complete unittest signature-derived msg positions, negative/positive fixtures and both repository assertions |
| API `repository_checks/test_repository_root_posture.py` | same basename | 4 | Source-layout depth, named roots and generic ROOT, aliases and inherited scopes, legal nonrepository roots, check-self and all Python scans |
| API `repository_checks/test_runtime_policy_posture.py` | same basename | 8 | Direct uppercase module bindings including conditional bindings, exact single-binding escape, declaration/local/class negatives, assigned-value privacy, exact historical API migration grammar and negatives; local production scan |
| API `repository_checks/test_error_transparency_posture.py` | same basename | 8 | Fixed OSError-to-CommandError translation even when chained; broad caught exception raw formatting; safe cause projections; block traversal and scope negatives; production scans |
| API `repository_checks/test_python_cache_clutter.py` | same basename | 2 | Every declared mandatory root must exist; root and nested Python/tool caches, pyc and pyo absent. Optional future package caches enter the scan; mandatory-root checks remain strict |
| API `repository_checks/test_workflow_posture.py` | same basename | 5 | Original mandatory presence maps to the reviewed local CI inventory (see deferred CI disposition); full external action commit pins, no dispatch defaults, no blank environment entries and explicit ubuntu-24.04 runner remain; one added unreviewed-workflow fixture |
| Dapp `tests/checks/silent_defaults/test_python.py` | `silent_defaults/test_python.py` | 4 | All environment/mapping/argparse defaults, generic truthiness/conditional defaults, env branch/KeyError and postparse fallback, concrete function defaults, except pass/concrete returns; explicit absence negatives; full authored inventory assertions |
| Dapp `tests/checks/silent_defaults/test_ops.py` | `silent_defaults/test_ops.py` | 6 | Make conditional/function defaults, shell default expansions and silent success, Docker ARG/ENV/default forms, dotenv and Compose blanks, workflow defaults; all representative fixtures and exact allowed-default inventories; one additional multiline ENV proof |
| Dapp `tests/checks/silent_defaults/test_shared_scanner.py` | `silent_defaults/test_shared_scanner.py` | 3 | Recursive discovery, checker-fixture and dependency exclusion, comments ignored and exact relative-line diagnostics |
| Vote `repo-checks/silent-defaults/test_python_empty_defaults.py` | `silent_defaults/test_python_empty_defaults.py` | 13 | All original env fallback, falsy values/constructors, explicit absence, empty get/pop source tag, zero getattr detector fixtures and five production scan assertions |
| Vote `repo-checks/silent-defaults/test_make_empty_defaults.py` | `silent_defaults/test_make_empty_defaults.py` | 8 | Conditional assignments, function fallback, same-variable empty-check assignment, target-specific operators and modifiers; all four source assertions and fixture groups |
| Vote `repo-checks/silent-defaults/test_shell_empty_defaults.py` | `silent_defaults/test_shell_empty_defaults.py` | 4 | All empty/default assignment expansion forms, strict-required negatives, private-secret/example separation, complete operator source scan |
| Vote `repo-checks/silent-defaults/test_env_file_empty_defaults.py` | `silent_defaults/test_env_file_empty_defaults.py` | 7 | Blank assignment forms, interpolation defaults, secret/example separation, relative exclusion validation, checker fixture exclusion, both source scans |
| Vote `repo-checks/silent-defaults/test_compose_empty_defaults.py` | `silent_defaults/test_compose_empty_defaults.py` | 3 | Default interpolation fixtures, required interpolation negatives, complete Compose scan |
| Vote `repo-checks/silent-defaults/test_dockerfile_empty_defaults.py` | `silent_defaults/test_dockerfile_empty_defaults.py` | 4 | ARG defaults, both ENV syntaxes and quoting, both complete source scans |
| Vote `repo-checks/silent-defaults/test_workflow_empty_defaults.py` | `silent_defaults/test_workflow_empty_defaults.py` | 4 | Workflow defaults, blank env-block classification, top-level/other-block negatives and both source scans |

Dapp default allowances are project-specific deployment values: all three local
allowance inventories are empty and their exact-inventory assertions remain.
The dapp-only COMPOSE_DOWN_CLEANUP silent-success exception was removed. Vote
private deployment filenames do not exist here; no local private dotenv file
exemption was introduced. The two original secret/example separation tests now
exercise the same contract against temporary private/example files. Vote's
original empty-get/pop matcher semantics remain unchanged. An empty-get tag
alone does not suppress the stronger mapping-default detector. The reviewed
pop-tag handling above does not waive Vote's separate production ENV-default
rule, which remains strict.

The source dapp scanner tokenizes ENV physical lines. Valid multiline local ENV
instructions require continuation joining before tokenization. Both its finding
and exact-inventory paths now retain every token and the first physical line;
an additional detector fixture proves the three continued defaults are all
reported, including continued instructions crossing blank/comment lines without
losing later tokens or the first physical line. The source changed-Make-default fixture receives its original
allowance only inside patch.dict during that fixture, never in repository scans.

The historical migration exemption remains limited to the source's exact
`nmr_api/.../migrations/NUMBER_NAME.py` grammar. It cannot exempt local tools,
checks, or nmrnet_inference modules. CommandError detection retains its exact
default Django CLI projection assumption; it was not broadened to arbitrary
exception classes.

## Make and tree assertion mappings

`test_make_hygiene.py` has 10 methods from the dapp source's 31-method hygiene
module. The copied original groups are Python bytecode, first-party symlinks,
help inventory equality, hierarchical targets, supported aggregate campaigns,
one root Makefile, exact phony inventory, explicit help default, and unique
targets. The forbidden-text method's secret patterns remain a separate tenth
method. Names map from `tests`/dapp campaigns to the local `test` aggregate's
exact `test/unit test/repository` prerequisites; its recipe must remain empty.
Dapp external-network/disposable live scenario disclosures have no counterpart
in this local offline aggregate.

The remaining dapp methods are domain-specific: Foundry input permissions,
recorded artifact branding exceptions and their negative fixtures, JS and Forge
build outputs, staged Solidity formatting, package CI campaigns, ABI closure,
Bike token/admin roles and deploy warning, Compose project/UID/update guards,
Bike CAM and viewer mock inputs, integration-fuzz descriptor binds, package
manifest discovery, Cast RPC secret input, GUI published bind and Anvil port.
Branding and license-marker prohibition are not local policy; the upstream
snapshot includes its license. Generic symlink discovery is covered by the
ported exact-source inventory helper. No JavaScript/TypeScript defaults module
was transplanted because no authored JS/TS surface exists here.

`test_makefile.py` retains 11 of the API source's 13 methods: all nine
MakeSourceInspectionTests, its MakeHelpTests method and its recipe-ownership
method. `_makefile.py` is copied as `makefile.py`. Missing/duplicate define and
recipe handling, target/prerequisite distinction, grouped/inline/double-colon
rejection, gap preservation and exact direct-owner discovery remain unchanged.
The ownership method maps credential publication to `packages/image`, hostile
macro override to hostile SHELL, and destructive default-goal substitution to
`weights/download`; all three original override declarations remain required.

The dapp default-goal assertion now accepts one bare or `override` assignment
to `help`. Its purpose is explicit safe discovery; API's ownership modifier
additionally resists hostile caller assignments. A separate assignment count
rejects duplicate authorities even if a second value is not a valid literal.
Two added fixtures retain changed/missing-default rejection and prove both
accepted spellings and duplicate rejection. The actual GNU Make hostile-caller
proof is unchanged; this resolves spelling conflict without weakening ownership.
The two API DeploymentDiagnosticsTests assert specific Compose project labels
and three API deployment names; there is no corresponding deployment owner.

`test_source_inventory.py` maps the API source's 10 methods into 13 methods:

| Original assertion | Disposition |
| --- | --- |
| Four SourceInventoryUnitTests | Copied unchanged, including recursive inventory, directional delta, exact missing/unclassified diagnostics, root grammar and file/directory symlink rejection |
| Exact Compose inventory/interpolation | Inapplicable: no local Compose source or service declarations |
| Exact container inventory and generic hardening | Local independent reviewed inventory; selected local engine built-in frontend with no external syntax fetch (negative pinned and unpinned directive probes); direct identical digest FROMs and final nonroot USER. Original no ADD/broad COPY/cache-secret-SSH mounts and other generic predicates retained |
| No CMD/ENTRYPOINT within that hardening method | API Compose owns commands; local Docker images own their commands. These two domain assertions are explicitly not transplanted |
| Compose build coverage of Dockerfiles | Inapplicable: direct launcher/build-script consumers, no Compose build-service graph |
| Checker complete safe source trees | Complete required read-only root grants, no Docker socket, safe recursive authored roots, mandatory host root metadata versus actually visible sources |
| Inventory authorities independent of discovery | Preserved: literal reviewed inventories and exact Dockerfile/support-file union |
| Deterministic serializable inventory | Preserved for the local container inventory |
| Additional visibility fixtures | Missing novel source/cache, hidden root symlink, stale exposed entry, malformed/duplicate records and type disagreement |

`source_inventory.py` retains every original helper predicate and failure. A
host `find` manifest at `/repository-source-inventory/root-entries` uses
`%y\t%f\0` records. Public entries absent from `/workspace` fail visibly,
including future root caches and new files. Checkout control (.git/.agents/
.codex), credentials (.aws) and writable acquisition/runtime state (.work) are
explicitly classified without granting their contents. This metadata assertion
must not be replaced with the already-mounted entries as its own authority.

## Execution requirements and expected discussion failures

### Deferred CI disposition (2026-10-14)

The API's unconditional workflow-presence assertion protects its adopted CI
lane. This repository explicitly defers CI; when adopted, it will contain
minimal critical checks (notes/011). The local presence group now checks an
independently reviewed exact workflow inventory, initially empty. A negative
fixture proves that an unreviewed workflow cannot expand that scope. All four
action-pin, dispatch-default, environment and runner predicates remain unchanged.
This is an explicit local applicability decision, not missing-tool forgiveness
or a passing claim about CI execution. The original API source and revision
remain recorded in test_workflow_posture.py and the assertion crosswalk above.

These modules use Python 3.12 standard library only. Make parser behavior probes
require `make` in the checker image; a missing executable remains a failure.
Launcher print probes require Bash and ordinary id utilities. They invoke no
engine. The checker requires complete read-only public inputs and the host root
entry metadata file. `test_conventions.py`'s exact mount fixture was expanded
for those justified grants, with no writable state mount.

Initial port findings included layout-derived roots, default/function fallback code, reversed
assertions, absent help/default ownership, absent workflows, missing Make,
missing Dockerfile frontend pins/direct FROM digests or nonroot declarations
and imported detector code failing a stronger independent ported rule.
These are historical port findings, not the current repair inventory; note 011
records completed fixes and applicability decisions. The initial port made no
production correction or policy exception.

## Remaining current API repository-check modules

This is the complete disposition of the current API modules not copied above.
It records missing product owners instead of creating equivalent-looking API
deployment, credential or database machinery in NMRNet. Container/scientific
families remain separately owned parts of the overall port; a handoff in this
table is not a claim that this directory already implements their assertions.

| API repository_checks module | Full group disposition |
| --- | --- |
| `test_admin_browser_lock_refresh.py` | Browser package lock Make publication/preservation and failure probes; no browser dependency or browser lock producer here |
| `test_advisory_lock_inventory.py` | Exact 13 PostgreSQL advisory call owners, complete two-int owner classification, fixed-key uniqueness, one security-event sharded domain and fixed/sharded disjointness; no PostgreSQL, SQL advisory locks or security-event shard owner here |
| `test_api_shutdown_grace.py` | Derived API server, persistence and Compose stop grace values; no ASGI/PostgreSQL deployed server here |
| `test_container_posture.py` | Generic source inventory/hardening/pins/Make/workflow groups overlap copied source checks and the separately owned container family. API-specific exact Compose service/profile/credential/storage/retention/routing/network/secret/shutdown/readiness inventories have no local API deployment owner |
| `test_credential_make_behavior.py` | Credential creation/publication and development-database Make behavior; no API credential producer or Django deployment database here |
| `test_dependency_lock.py` | Exact API lock-refresh tool/image inputs, flock/concurrent publication, atomic replacement and rollback; scientific/container port owner must assess equivalent local packages/lock lanes. No API dependency table or refresh Docker service is transplanted |
| `test_dependency_versions.py` | Exact API/GUI shared version table consumed by Make/Docker/Python metadata, literal image pins and embedded toolchain alignment; generic pin/consumer alignment assigned to scientific/container port owner, fixed API versions and packages have no local owner |
| `test_development_postgres_reconfiguration.py` | Compose replacement hashes and permitted PostgreSQL changes; no PostgreSQL deployment here |
| `test_disposable_api.py` | Disposable rendering of API deployment authority and module import/fake engine probes; no corresponding API renderer here |
| `test_generated_artifact_posture.py` | Exact setuptools package_data and regular-file inventories for public/provider OpenAPI and user-signing schemas; six named renderer/check/write Make lanes, nonroot guard, current writer UID/GID and failure before OpenAPI validation. No generated schema package or schema producer exists here; launcher nonroot authority is separately checked, not implemented by inventing schema render targets |
| `test_http_exchange_capacity.py` | API HTTP exchange concurrency derived from Uvicorn worker/admission configuration; no API HTTP exchange runtime here |
| `test_integration_facade.py` | Exact coordinator interface for dev-local/dev/run topologies and its manifests; no such API integration facade here |
| `test_integration_facade_behavior.py` | Exact API topology lifecycle admission, locking and mutation authority probes; no local topology lifecycle facade |
| `test_operator_logs.py` | Exact dev-local/dev/run service log views, surviving public/provider/private worker choices, absent/foreign deployment refusal and inspection cause preservation; no deployed API service log target here |
| `test_operator_security_events.py` | Exact API SecurityEvent lookup command, option/glob request ID data, required ID, fallback API selection, unique-container ownership, foreign-checkout and command-cause preservation; no SecurityEvent store or lookup operator target here |
| `test_persistence_connection_budget.py` | Configured API persistence lanes fit deployed PostgreSQL budgets; no PostgreSQL/persistence composition owner |
| `test_persistence_execution_boundary.py` | Exact reviewed sync_to_async owners, ASGI entrypoint/composition import ownership, persistence runtime backend and explicit Uvicorn lifespan; no Django/asgiref/ASGI persistence runtime here |
| `test_postgres_test_lifecycle.py` | Real Make PostgreSQL lane cleanup and concurrent checkout locking; no PostgreSQL test lane |
| `test_provider_release_make.py` | Same clean committed revision before/after provider image build and scoped release writes; scientific source/build-closure owner must assess local inference-image correspondence. No provider release/deployment publisher is invented |
| `test_run_image_build_behavior.py` | Committed source archive and selected revision, dirty/invalid source rejection before build, drift and HEAD change during build; handed to scientific source/build-closure owner for local inference image consumer assessment |
| `test_run_operator_posture.py` | Exact image-only edge-routed API Compose run lane, declared runtime inputs and command guards; no edge-routed API deployment here |
| `test_run_preflight_configuration.py` | Uvicorn module probes cover exact ASGI env/mount contracts; no Uvicorn server composition here. Scientific import proof is a different owner, not a literal substitute |
| `test_run_storage_lifecycle.py` | Persistent API storage reset only when stopped, shared lifecycle lock, capability-free reset service and refusal before mutation; no API persistent-storage reset owner |
| `test_starter_demo_analysis_wiring.py` | Optional API demo analysis provider registration and composition wiring; no demo API provider package |
| `test_upload_cleanup_wiring.py` | Automatic API Upload cleanup worker with restricted database/private-store authority; no Upload store/cleanup worker |
| `test_user_and_provider_credential_wiring.py` | API user/managed-provider signer, delivery and registration authority wiring; no such credential provisioning owner |
| `test_user_credential_cleanup_wiring.py` | API credential-delivery cleanup under continuously supervised deployment; no corresponding delivery/cleanup process |

All API `tests/repo-checks` transparency modules are runtime API behavior:
`test_configuration_transparency`, `test_control_status_evidence`,
`test_error_transparency`, `test_finalize_storage_transparency`,
`test_parser_failure_transparency`, `test_replay_capacity_transparency`,
`test_retained_capacity_transparency`, `test_route_memory_transparency`,
`test_service_error_transparency`, `test_signature_processing_transparency`,
`test_transfer_admission_transparency`, `test_transfer_filesystem_transparency`,
`test_upload_set_message_transparency`, and `test_user_error_messages` require
Django request/services/problem schemas, credential/signature/security state,
Upload storage or API capacity owners absent here. Their safe-cause expectation
does not authorize copying those product models. `test_import_boundaries` and
`test_package_exports` assert specific API route/security/operations/composition
package owners absent here. `test_test_taxonomy` hardcodes the API test taxonomy;
it is not a universal test organization rule. Local discovery completeness stays
with the two local suites, without adopting that different taxonomy.

## Foundation repair: runtime proof arguments

The local kernel-grant proof in `../test_container_port_runtime.py` retains all
eight runtime methods from Magnet
`repository_checks/test_container_posture.py` at
`2d4ee50b0359e71900fce9289ea01f46d0f41249`:
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_container_posture.py

Its unused layout-derived `ROOT` binding was removed because these checks read
kernel and cgroup state, not repository inputs. Assertions place the launcher's
expected identity and resource grants before observations, and use explicit
`msg=` diagnostics for identity, resource and retained-build-source failures.
The independent expected UID/GID, read-only root, hardened writable tmpfs,
loopback-only networking, zero effective capabilities, no-new-privileges,
resource limits and missing build-source assertions are unchanged. No detector
predicate or default allowance changed.

## Foundation repair: explicit Python scan responsibility

The local `sources.python_sources` helper now requires the keyword-only
`production_only` choice. Root-layout and tuple checks explicitly choose the full
authored inventory; runtime-policy, validation-guard and error-transparency checks
retain their explicit production selection. Both projections still include every
nested future `nmrnet_inference` module. No inventory root or detector predicate
changed. The literal branch that selects the inventory remains a classification,
not a substitute for a missing operator input.

## Foundation repair: scientific checker repository authority

The dependency, fixture-provenance and scientific-product contract modules now
borrow the contained lane's pinned root from `quality.sources`, rather than
inferring a repository from their Python package depth. Their selected input
paths are unchanged under the maintained `/workspace` grant. Expected declaration,
lock and solvent facts appear before observations in equality assertions. The
original NMRPeak dependency/product and SECS fixture obligations remain unchanged;
no lock grammar, scientific selector, provenance predicate or default allowance
changed. Fixture-provenance prose now reflects the earned producer and attribution
bindings rather than its initial missing-evidence state.
