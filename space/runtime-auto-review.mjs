import fs from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
function auditReview(exec, state, decision) {
    const row = {time:new Date().toISOString(), session_id:exec.agent?.session?.id,
        root_call_id:exec.rootCallId, tool:exec.name, state,
        ...(decision ? {risk:decision.risk, decision:decision.decision} : {})};
    // Metadata only. Never save tool arguments, prompts, secrets or reviewer text.
    try { fs.appendFileSync(fileURLToPath(new URL('./data/auto-review-audit.jsonl',import.meta.url)), JSON.stringify(row)+'\n'); }
    catch { /* Diagnostic failure must not change the reviewer's decision. */ }
}
// Resolve the modules from the installed runtime rather than a hard-coded
// Windows path. DSH_RUNTIME_PACKAGE points at .../dsh/package.json, so this
// works on both the Linux container and a Windows host install.
const runtimeRequire=createRequire(process.env.DSH_RUNTIME_PACKAGE||'/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json');
const { BlockAssembler } = await import(pathToFileURL(runtimeRequire.resolve('@deepseek-ai/dsh-llm')).href);
const { deepFreeze } = await import(pathToFileURL(runtimeRequire.resolve('@deepseek-ai/dsh-util-values')).href);
const { AUTO_PRESET } = await import(pathToFileURL(runtimeRequire.resolve('@deepseek-ai/dsh-permission-presets')).href);
const { RUN_CODE_NAME } = await import(pathToFileURL(runtimeRequire.resolve('@deepseek-ai/dsh-tools')).href);
//#region lib/types/index.js
/**
* LLM-backed authorization gate for the current-session-only Auto permission
* preset. Every native call and every started PTC inner call is reviewed once
* before its body; the outer `run_code` transport is deliberately excluded.
* Under the `ask` approval policy a reviewer denial asks the user; under
* `never` it is final.
*
* @module @deepseek-ai/dsh-experimental-auto-review
*/
/** Structured error name persisted for every final reviewer denial. */
const AUTO_REVIEW_DENIED_ERROR_NAME = "AutoReviewDeniedError";
/** Structured error code persisted for every final reviewer denial. */
const AUTO_REVIEW_DENIED_CODE = "AUTO_REVIEW_DENIED";
/** Owner 2026-10-23: 自动审查要薄 —— 只判这一次调用的实际后果，默认放行。 */
const REVIEW_POLICY = `REVIEW_POLICY
这是唯一一道很薄的拦截：只判这一个待执行的工具调用，不审历史、不评判主人是否授权过、不看语气和自称。默认放行（allow）。

只在这些"确定会造成不可挽回后果"的情况下才 deny（其余全部 allow）：
- 不可恢复地删除或覆盖工作区之外的既有数据；
- 把凭据、密钥或私密数据发往外部不可信目的地；
- 未经设备所有者同意去操作别人的设备或账号；
- 让主人付出不可逆代价：转账、付款、以他人名义公开发言。

只返回一个 JSON 对象，两种形状：
{"risk":"low","decision":"allow"}
{"risk":"high","decision":"deny","reason":"..."}
拿不准时返回 allow —— 薄边界只挡确定有害的动作，不额外增加授权关卡。`;

// 薄边界：下面这些工具只读本地状态，不可能造成不可挽回后果，
// 直接放行，不花一次审查模型调用（自动审查档在多数步骤里因此是零成本）。
const THIN_SAFE = new Set([
	"read", "glob", "grep", "todo_write", "working_directory",
	"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
	"job_list", "job_output", "skill", "load_workspace_dependencies", "present"
]);
/** Cordis plugin name used by loader diagnostics. */
const name = "experimental-auto-review";
/** Complete host services required before Auto may be advertised. */
const inject = [
	"approval",
	"llm",
	"permissionPresets",
	"sessions",
	"tools",
];
/** Return JSON text for one immutable logged value. */
function json(value) {
	const rendered = JSON.stringify(value, null, 2);
	/* v8 ignore next -- accepted Session facts and frozen review snapshots are lossless JSON by contract. */
	if (rendered === void 0) throw new Error("auto-review: a required value is not JSON-serializable");
	return rendered;
}
/** Recreate the agent-loop's parse of one native call's logged raw arguments. */
function parseLoggedArguments(raw) {
	if (raw === "") return {};
	try {
		return JSON.parse(raw);
	} catch {
		return raw;
	}
}
/** Compare two lossless-JSON values without retaining aliases. */
function sameJson(left, right) {
	return JSON.stringify(left) === JSON.stringify(right);
}
/** Whether one logged JSON value is an object record rather than null or an array. */
function isRecord(value) {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}
/** Validate the schema fields that must be present in a logged pending action. */
function loggedSchema(value, expectedName, mode) {
	if (typeof value.description !== "string" || !isRecord(value.parameters)) throw new Error(`auto-review: the pending ${mode} tool schema is incomplete`);
	return {
		name: expectedName,
		description: value.description,
		parameters: value.parameters
	};
}
/** Read live abort state across awaits without relying on stale narrowing. */
function isAborted(signal) {
	return signal.aborted;
}
/** Whether this visible message is a durable shipped-Web human instruction. */
function isHumanInstruction(source) {
	return source.kind === "user" && typeof source.rpcId === "string";
}
/** Whether this visible context is the current project-instruction source. */
function isProjectInstruction(source) {
	return source.kind === "agent-instructions";
}
/** Whether this source is a compaction checkpoint. */
function isCheckpoint(source) {
	return source.kind === "compact-checkpoint";
}
/** Whether this message was durably attributed to the child's direct parent. */
function isDirectParentInstruction(source, parentSession) {
	return parentSession !== void 0 && source.kind === "agent-message" && source.senderSessionId === parentSession;
}
/** Find the visible-role identity of the in-process child's creation prompt. */
function directParentInitialPromptSeq(agent, events) {
	const { session } = agent;
	if (session.header.origin !== "subagent" || session.header.parentSession === void 0) return void 0;
	let passedCreationBoundary = false;
	for (const event of events) {
		if (!session.isOwnSeq(event.seq)) continue;
		if (event.type === "subagent/descriptor") {
			passedCreationBoundary = true;
			continue;
		}
		if (passedCreationBoundary && event.type === "user/message" && event.data.source.kind === "user" && !isHumanInstruction(event.data.source)) return event.seq;
	}
}
/** Assign one retained text block its fixed instruction, constraint, summary, or fact role. */
function textRole(source, seq, initialPromptSeq, parentSession) {
	if (isHumanInstruction(source)) return "human-instruction";
	if (seq === initialPromptSeq || isDirectParentInstruction(source, parentSession)) return "direct-parent-instruction";
	if (isCheckpoint(source)) return "checkpoint";
	return "fact";
}
/** Partition one visible user-role message into role-labelled retained blocks. */
function filteredUserEntries(seq, source, content, initialPromptSeq, parentSession) {
	return content.map((block) => ({
		kind: "user-message",
		role: block.type === "text" ? textRole(source, seq, initialPromptSeq, parentSession) : "fact",
		source,
		content: [block]
	}));
}
/** Copy the turn and step identity carried by one core execution event. */
function stepIdentity(data) {
	return {
		turn: data.turn,
		step: data.step
	};
}
/** Compare two turn-and-step identities. */
function sameStep(left, right) {
	return left.turn === right.turn && left.step === right.step;
}
/** Key one call id inside the step that owns its lifecycle. */
function scopedCallKey(step, callId) {
	return `${step.turn}\0${step.step}\0${callId}`;
}
/** Assign each PTC start to the step open when it was logged. */
function scopePtcStarts(events) {
	const starts = [];
	let openStep;
	for (const event of events) {
		if (event.type === "turn/start" || event.type === "turn/end") {
			openStep = void 0;
			continue;
		}
		if (event.type === "step/start") {
			openStep = stepIdentity(event.data);
			continue;
		}
		if (event.type === "step/end") {
			openStep = void 0;
			continue;
		}
		if (event.type !== "tool/ptc-dispatch-start") continue;
		if (openStep === void 0) throw new Error("auto-review: a PTC call has no owning step in the session log");
		starts.push({
			event,
			step: openStep
		});
	}
	return {
		starts,
		openStep
	};
}
/** Resolve one native action from its visible call and latest request header. */
function nativeAction(exec, headerTools, logged) {
	if (logged.data.name !== exec.name || !sameJson(parseLoggedArguments(logged.data.arguments), exec.arguments)) throw new Error("auto-review: the pending native call disagrees with its logged action");
	const schemas = (Array.isArray(headerTools) ? headerTools : []).filter((schema) => isRecord(schema) && schema["name"] === exec.name);
	const [candidate] = schemas;
	if (candidate === void 0 || schemas.length !== 1) throw new Error("auto-review: the pending native tool schema is missing or ambiguous");
	const schema = loggedSchema(candidate, exec.name, "native");
	return {
		mode: "native",
		name: schema.name,
		description: schema.description,
		parameters: schema.parameters,
		arguments: exec.arguments
	};
}
/** Resolve a PTC inner action from its binding schema and logged identity. */
function ptcAction(exec, start, visibleParentKeys) {
	const { event } = start;
	if (!visibleParentKeys.has(scopedCallKey(start.step, event.data.parentCallId)) || event.data.rootCallId !== exec.rootCallId || event.data.name !== exec.name || !sameJson(event.data.arguments, exec.arguments)) throw new Error("auto-review: the pending PTC call disagrees with its logged action");
	if (exec.schema === void 0 || exec.schema.name !== exec.name) throw new Error("auto-review: the pending PTC binding schema is missing or inconsistent");
	const schema = loggedSchema(exec.schema, exec.name, "PTC");
	return {
		mode: "ptc-inner",
		name: schema.name,
		description: schema.description,
		parameters: schema.parameters,
		arguments: exec.arguments
	};
}
/**
* Freeze the five reviewer sections from one session and pending execution.
* @param agent - agent whose durable surface and request header authorize the call.
* @param exec - immutable pending execution.
* @returns the exact route and four data sections paired with {@link REVIEW_POLICY}.
*/
function snapshotAutoReview(agent, exec, cwd) {
	const { session } = agent;
	const events = session.snapshotEvents();
	const nodes = [...session.surface.nodes];
	const header = session.requestHeader();
	if (header === void 0 || header.config.provider.length === 0 || header.config.model.length === 0) throw new Error("auto-review: no complete request-header route is available");
	const nativeCalls = events.filter((event) => event.type === "tool/call");
	const { starts, openStep: currentStep } = scopePtcStarts(events);
	const initialPromptSeq = directParentInitialPromptSeq(agent, events);
	const nativeByScopedId = /* @__PURE__ */ new Map();
	for (const event of nativeCalls) {
		const key = scopedCallKey(stepIdentity(event.data), event.data.callId);
		const bucket = nativeByScopedId.get(key);
		if (bucket === void 0) nativeByScopedId.set(key, [event]);
		else bucket.push(event);
	}
	const startsByParent = /* @__PURE__ */ new Map();
	const startsBySubCall = /* @__PURE__ */ new Map();
	for (const start of starts) {
		const subCallKey = scopedCallKey(start.step, start.event.data.subCallId);
		if (startsBySubCall.has(subCallKey)) throw new Error("auto-review: a PTC call identity is ambiguous in the session log");
		startsBySubCall.set(subCallKey, start);
		const parentKey = scopedCallKey(start.step, start.event.data.parentCallId);
		const bucket = startsByParent.get(parentKey);
		if (bucket === void 0) startsByParent.set(parentKey, [start]);
		else bucket.push(start);
	}
	if (currentStep === void 0) throw new Error("auto-review: the pending call has no open step in the session log");
	const currentRootCalls = nativeByScopedId.get(scopedCallKey(currentStep, exec.rootCallId)) ?? [];
	const currentRootCall = currentRootCalls[0];
	if (currentRootCall === void 0 || currentRootCalls.length !== 1) throw new Error("auto-review: the pending root call is missing or ambiguous in the session log");
	const currentPtcStart = exec.parent === void 0 ? void 0 : startsBySubCall.get(scopedCallKey(currentStep, exec.callId));
	if (exec.parent !== void 0 && currentPtcStart === void 0) throw new Error("auto-review: the pending PTC call is missing or ambiguous in the session log");
	const projectInstructions = [];
	const history = [];
	const visibleParentKeys = /* @__PURE__ */ new Set();
	let passedCurrentRoot = false;
	for (const seq of nodes) {
		const event = events[seq];
		if (event.type === "user/message") {
			if (event.data.source.kind === "tool") continue;
			if (isProjectInstruction(event.data.source)) {
				const content = event.data.content;
				if (content.length > 0) projectInstructions.push({
					kind: "user-message",
					role: "constraint",
					source: event.data.source,
					content
				});
			} else history.push(...filteredUserEntries(event.seq, event.data.source, event.data.content, initialPromptSeq, session.header.parentSession));
			continue;
		}
		if (event.type !== "assistant/message") continue;
		const messageStep = stepIdentity(event.data);
		const isCurrentMessage = sameStep(messageStep, currentStep);
		let sawUnstartedSibling = false;
		for (const block of event.data.message.content) {
			if (block.type !== "tool-call") continue;
			const key = scopedCallKey(messageStep, block.id);
			const isCurrentRoot = isCurrentMessage && block.id === exec.rootCallId;
			if (isCurrentRoot && passedCurrentRoot) throw new Error("auto-review: the pending root call is ambiguous in the current surface");
			const calls = nativeByScopedId.get(key) ?? [];
			if (calls.length > 1) throw new Error("auto-review: a native call identity is ambiguous in the session log");
			const call = calls[0];
			const startsForCall = startsByParent.get(key) ?? [];
			if (call === void 0) {
				if (isCurrentMessage && !passedCurrentRoot) throw new Error("auto-review: a visible call before the pending root is missing from the session log");
				if (startsForCall.length > 0) throw new Error("auto-review: an unstarted visible call has logged PTC dispatches");
				sawUnstartedSibling = true;
				continue;
			}
			if (sawUnstartedSibling) throw new Error("auto-review: visible native call logs do not form a started prefix");
			if (call.data.name !== block.name || call.data.arguments !== block.arguments) throw new Error("auto-review: a visible tool call disagrees with its logged action");
			visibleParentKeys.add(key);
			if (call !== currentRootCall || exec.parent !== void 0) history.push({
				kind: "tool-call",
				role: "fact",
				mode: "native",
				name: call.data.name,
				arguments: call.data.arguments
			});
			for (const start of startsForCall) {
				if (start === currentPtcStart) continue;
				history.push({
					kind: "tool-call",
					role: "fact",
					mode: "ptc-inner",
					name: start.event.data.name,
					arguments: json(start.event.data.arguments)
				});
			}
			if (isCurrentRoot) passedCurrentRoot = true;
		}
	}
	if (!passedCurrentRoot) throw new Error("auto-review: the pending root call is missing from the current surface");
	const action = exec.parent === void 0 ? nativeAction(exec, header.tools, currentRootCall) : ptcAction(exec, currentPtcStart, visibleParentKeys);
	return deepFreeze({
		provider: header.config.provider,
		model: header.config.model,
		cwd,
		projectInstructions,
		history,
		action
	});
}
/** Render the single pending action (plus cwd) for the thin reviewer. */
function reviewUserText(snapshot) {
	return [
		"ENVIRONMENT",
		json({ cwd: snapshot.cwd }),
		"PENDING_ACTION",
		json(snapshot.action)
	].join("\n\n");
}
/** Count members in the raw top-level JSON object. */
function topLevelMemberCount(text) {
	const syntax = text.replace(/"(?:\\.|[^"\\])*"/gs, "");
	let depth = 0;
	let count = 0;
	for (const char of syntax) switch (char) {
		case "{":
		case "[":
			depth += 1;
			break;
		case "}":
		case "]":
			depth -= 1;
			break;
		case ":": if (depth === 1) count += 1;
	}
	return count;
}
/** Parse the closed risk/decision protocol and its fixed safety combinations. */
function parseDecision(text) {
	const value = JSON.parse(text);
	if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("auto-review: reviewer output must be one JSON object");
	const record = value;
	const keys = Object.keys(record);
	if (topLevelMemberCount(text) !== keys.length) throw new Error("auto-review: reviewer output repeats a JSON member");
	const risk = record["risk"];
	const decision = record["decision"];
	if (keys.length === 2 && decision === "allow" && (risk === "low" || risk === "medium")) return {
		risk,
		decision
	};
	if (keys.length === 2 && decision === "deny" && (risk === "medium" || risk === "high")) return {
		risk,
		decision
	};
	if (decision === "deny" && (risk === "medium" || risk === "high") && keys.length === 3 && Object.hasOwn(record, "reason") && typeof record["reason"] === "string") return {
		risk,
		decision,
		reason: record["reason"]
	};
	throw new Error("auto-review: reviewer output does not match the risk/decision protocol");
}
/** Consume zero or more reasoning blocks, one JSON text block, and one terminal stop. */
async function readDecision(stream) {
	const assembler = new BlockAssembler();
	let finished = false;
	for await (const chunk of stream) {
		if (finished) throw new Error("auto-review: reviewer emitted data after its terminal finish");
		assembler.push(chunk);
		if (chunk.type === "finish") {
			finished = true;
			if (chunk.reason.kind === "error" || chunk.reason.kind === "aborted") {
				const { code, message } = chunk.reason.failure;
				throw new Error(`auto-review: reviewer ended with ${chunk.reason.kind} ${code}: ${message}`);
			}
			if (chunk.reason.kind !== "stop") throw new Error(`auto-review: reviewer ended with ${chunk.reason.kind}`);
		}
	}
	if (!finished) throw new Error("auto-review: reviewer emitted no terminal finish");
	const blocks = assembler.blocks();
	const final = blocks.at(-1);
	if (final?.type !== "text" || blocks.slice(0, -1).some((block) => block.type !== "reasoning")) throw new Error("auto-review: reviewer must emit zero or more reasoning blocks followed by exactly one text block");
	return parseDecision(final.text);
}
/** Review one frozen pending action with the fixed policy and current LLM route. */
async function classifyRisk(ctx, agent, exec, signal) {
	// The space profile intentionally disables workspace-controller, so the
	// workingDirectory service is not composed. The reviewer only needs the
	// session cwd for its prompt; it never reads files through this service.
	const snapshot = snapshotAutoReview(agent, exec,
		agent?.session?.header?.cwd || '/workspace');
	const options = deepFreeze({
		sessionId: agent.session.id,
		reasoningEffort: agent.session.requestHeader().config.reasoningEffort,
		maxTokens: 1024,
		provider: snapshot.provider,
		model: snapshot.model,
		system: REVIEW_POLICY,
		messages: [{
			role: "user",
			content: [{
				type: "text",
				text: reviewUserText(snapshot)
			}]
		}],
		temperature: 0,
		signal
	});
	return readDecision(ctx.llm.stream(options));
}
/** Materialize the fixed model-facing final Auto denial plus optional UI detail. */
function denied(exec, reason) {
	return {
		kind: "deny",
		reason: `Auto review rejected tool "${exec.name}"; its body was not executed`,
		info: {
			name: AUTO_REVIEW_DENIED_ERROR_NAME,
			code: AUTO_REVIEW_DENIED_CODE,
			...reason === void 0 ? {} : { reason }
		}
	};
}
/**
* Ask the user to decide one call the reviewer denied. The audited reason is
* English; the prompt text is localized and keeps the reviewer's raw reason.
*/
function askUser(exec, reason) {
	const denial = `Auto review denied tool "${exec.name}"`;
	return {
		kind: "ask",
		reason: reason === void 0 ? denial : `${denial}: ${reason}`,
		displayReason: reason === void 0 ? {
			en: "Auto review denied this call.",
			zh: "Auto review 拒绝了此调用。"
		} : {
			en: `Auto review denied this call: ${reason}`,
			zh: `Auto review 拒绝了此调用：${reason}`
		}
	};
}
/** Materialize a reviewer failure as its own error rather than a denial. */
function failed(exec, error) {
	const message = error instanceof Error ? error.message : String(error);
	return {
		kind: "deny",
		reason: `Auto review of tool "${exec.name}" failed; its body was not executed: ${message}`
	};
}
/** Install the Auto preset and its prepended per-call review gate. */
function apply(ctx) {
	const permissionPresets = ctx.permissionPresets;
	let accepting = true;
	const active = /* @__PURE__ */ new Set();
	const lifecycle = new AbortController();
	ctx.effect(function* () {
		yield ctx.on("tools/pre-execute", async (exec, next) => {
			const agent = exec.agent;
			if (agent === void 0 || exec.parent === void 0 && exec.name === RUN_CODE_NAME) return next();
			if (permissionPresets.current(agent.session) !== AUTO_PRESET) return next();
			if (!accepting || lifecycle.signal.aborted) return { kind: "cancel" };
			// 薄边界：本地只读类工具直接放行，不调用审查模型。
			if (THIN_SAFE.has(exec.name)) {
				auditReview(exec, 'started');
				auditReview(exec, 'decided', { risk: 'low', decision: 'allow' });
				return next();
			}
			const completed = Promise.withResolvers();
			active.add(completed.promise);
			try {
				auditReview(exec, 'started');
				const review = await classifyRisk(ctx, agent, exec, AbortSignal.any([exec.signal, lifecycle.signal])).then((decision) => ({
					ok: true,
					decision
				}), (error) => ({
					ok: false,
					error
				}));
				if (isAborted(lifecycle.signal)) return { kind: "cancel" };
				if (!review.ok) { auditReview(exec, 'failed'); return failed(exec, review.error); }
				const { decision } = review;
				auditReview(exec, 'decided', decision);
				if (decision.decision === "deny" && ctx.approval.overrideOf(agent.session) === "never") return denied(exec, decision.reason);
				const downstream = await next();
				if (isAborted(lifecycle.signal)) return { kind: "cancel" };
				if (decision.decision === "allow" || downstream.kind !== "allow") return downstream;
				return askUser(exec, decision.reason);
			} finally {
				active.delete(completed.promise);
				completed.resolve();
			}
		}, { prepend: true });
		yield permissionPresets.registerAuto(() => {
			if (!accepting) throw new Error("auto-review: integration is closing");
		});
		yield async () => {
			accepting = false;
			try {
				for (const session of ctx.sessions.list()) {
					if (permissionPresets.current(session) !== AUTO_PRESET) continue;
					permissionPresets.set(session, "danger-full-access");
				}
			} finally {
				lifecycle.abort(/* @__PURE__ */ new Error("auto-review integration disposed"));
				await Promise.allSettled([...active]);
			}
		};
	}, "auto-review lifecycle");
}
//#endregion
export { apply, inject, name };
