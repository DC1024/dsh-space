// Compatibility shim for the `auto` permission identity.
//
// Why this file exists: the permission service, at init, walks every stored
// session and re-pins its persisted preset. A session that was saved as "auto"
// cannot be restored unless some integration has registered "auto" — DSH throws
// `permission: cannot restore preset "auto" without its active integration`
// (dsh-permission-presets/lib/index.js:205,370-376). The reviewer is the
// integration that owns "auto", so as long as a single old auto session exists
// the reviewer had to stay mounted in every preset, even where it must do
// nothing.
//
// This shim breaks that tie. It registers the "auto" identity with a no-op
// admission callback and installs no hooks at all: it never classifies, asks,
// blocks or logs a request. It is mounted only when the configured preset is
// not `auto` (see prepare_runtime.mjs), i.e. exactly when the reviewer is
// unmounted. The restored session boots under AUTO_PRESET_SPEC
// (danger-full-access + ask) and runtime-policy.mjs re-pins it to the
// configured preset on its next step, so the stale identity dies on first use.
//
// No file writes, no network, no LLM calls: the four lines below are the whole
// behaviour. It is not a security boundary and must never grow into one.
export const name = 'auto-preset-shim';
export const inject = ['permissionPresets'];

export function apply(ctx) {
  let accepting = true;
  ctx.effect(function* () {
    yield ctx.permissionPresets.registerAuto(() => {
      // A disposed integration must not admit new restorations; DSH calls the
      // admission callback again when a session is resumed after teardown.
      if (!accepting) throw new Error('auto-preset-shim: integration is closing');
    });
    yield async () => { accepting = false; };
  }, 'auto-preset-shim lifecycle');
}
