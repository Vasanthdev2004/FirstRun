# Controlled acceptance fixture

`notes-app/` is an intentionally incomplete public setup path for a synthetic
Node/SQLite service. The application and migration are real; the missing setup
step is deliberate. This is a test asset, not a claim of customer adoption.

Do not run this through a general FirstRun host-shell fallback. The implemented
product must execute its archive in the sandbox and run the protected HTTP probe
outside repository control. M0 must resolve the app image digest and validate the
separate trusted-probe network arrangement before declaring the lane runnable.

Only this repository subdirectory goes into the model's input; oracle and acceptance
expectations elsewhere in this pack stay outside. Package dependencies are absent,
so the fixture can install with npm offline after the runtime image is available.
Node's built-in SQLite module is used; the authoring check ran on Node v22.16.0.
