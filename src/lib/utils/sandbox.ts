/**
 * Sandbox link resolution.
 *
 * Models reference container-generated files with a `sandbox:` URI, e.g.
 *   [report](sandbox:/workspace/outputs/report.docx)
 *
 * `sandbox:` is not a navigable scheme and has no registered authority, so the
 * number of slashes after `sandbox:` carries no meaning and different models
 * emit different forms:
 *
 *   sandbox:/workspace/...      scheme + absolute path  (canonical; what we prompt for)
 *   sandbox://workspace/...     `workspace` parsed as an authority/host
 *   sandbox:///workspace/...    empty authority + absolute path (file:// style)
 *   sandbox:workspace/...       no slashes at all
 *
 * We normalise every form to the same relative path (e.g. `outputs/report.docx`)
 * so resolution never depends on which variant the model happened to produce.
 *
 * Files are stored with a relative `container_workspace.workspace_path` such as
 * `outputs/report.docx` or `inputs/data.csv` (see container_workspace.py), so the
 * normalised href is compared against the normalised stored path.
 *
 * NOTE: only the `sandbox:` scheme is treated as a sandbox link. A bare
 * `/workspace/...` path is NOT — `/workspace` is a real in-app route (the
 * Workspace section), so hijacking it would break navigation.
 */

const SANDBOX_SCHEME_RE = /^\s*sandbox:/i;

export const isSandboxHref = (href: string | undefined | null): boolean =>
	!!href && SANDBOX_SCHEME_RE.test(href);

export const normalizeSandboxPath = (value: string | undefined | null): string => {
	if (!value) return '';
	let out = String(value).trim();
	try {
		out = decodeURIComponent(out);
	} catch {
		// Malformed %-escape — keep the raw value rather than throwing.
	}
	return (
		out
			// Strip the sandbox scheme with ANY number of leading slashes
			// (sandbox:, sandbox:/, sandbox://, sandbox:///, …).
			.replace(/^sandbox:\/*/i, '')
			// Strip a leading `workspace` root segment (with or without a leading
			// slash), but only as a WHOLE path segment so a file literally named
			// `workspace_notes.docx` at the root is left intact.
			.replace(/^\/*workspace(?=\/|$)/i, '')
			// Drop any leftover leading slashes → a clean relative path.
			.replace(/^\/+/, '')
	);
};

// An HREF (unlike a stored filesystem path) may carry a URI fragment/query that
// marked keeps in token.href (e.g. `…/Foo.docx#summary`). hrefToRelPath strips it
// from the RAW href — using the LITERAL `#`/`?` delimiters, before decoding. But a
// container filename can ALSO legitimately contain `#`/`?` and models write it
// unencoded, so resolveSandboxFile tries the un-stripped literal path FIRST and
// only falls back to this stripped form for a genuine fragment.
const hrefToRelPath = (href: string | undefined | null): string =>
	normalizeSandboxPath(String(href ?? '').replace(/[?#].*$/, ''));

const fileWorkspacePath = (file: any): string =>
	normalizeSandboxPath(
		file?.container_workspace?.workspace_path ??
			file?.file?.data?.container_workspace?.workspace_path ??
			''
	);

const fileVersion = (file: any): number => {
	const v =
		file?.container_workspace?.version ?? file?.file?.data?.container_workspace?.version;
	const n = typeof v === 'number' ? v : Number(v);
	return Number.isFinite(n) ? n : 0;
};

// Of several descriptors for the same path, the newest one: highest version,
// and on a version tie the LAST array entry (import order ⇒ most recent).
const pickLatest = (files: any[]): any =>
	files.reduce((best, f) => (fileVersion(f) >= fileVersion(best) ? f : best), files[0]);

const basename = (path: string): string => path.split('/').pop() ?? '';

const topSegment = (path: string): string => (path.includes('/') ? path.split('/')[0] : '');

/**
 * Resolve a `sandbox:` href to the matching generated-file descriptor in
 * `sandboxFiles`, or null when no confident match exists.
 *
 * Matching is layered, most-specific first:
 *   1. exact normalised-path match (incl. an implicit `outputs/` prefix, since
 *      models sometimes drop the subdirectory). The LITERAL href path is tried
 *      first (so a file genuinely named `report#final.docx`, referenced with an
 *      unencoded `#`, wins), then the fragment-stripped path (so a genuine URI
 *      fragment like `Foo.docx#summary` also resolves). When several descriptors
 *      share the path (multiple imported versions), the NEWEST is returned.
 *   2. a UNIQUE basename match (right filename, slightly different directory) —
 *      only when exactly one generated file carries that basename AND it lives in
 *      the same top-level namespace (so an `inputs/x` link never silently opens
 *      an unrelated `outputs/x`).
 */
export const resolveSandboxFile = (
	href: string | undefined | null,
	sandboxFiles: any[]
): any | null => {
	if (!isSandboxHref(href)) return null;

	const files = Array.isArray(sandboxFiles) ? sandboxFiles : [];

	// Two readings of the href: the LITERAL path (keeps a `#`/`?` that is part of
	// the filename) and the FRAGMENT-STRIPPED path (for a genuine URI fragment).
	const relLiteral = normalizeSandboxPath(href);
	const relStripped = hrefToRelPath(href);

	const candidateGroups: string[][] = [];
	const pushGroup = (r: string) => {
		if (!r) return;
		const group = [r];
		// A model may omit the `outputs/` (or other) subdirectory it actually lives in.
		if (!r.startsWith('outputs/')) group.push(`outputs/${r}`);
		candidateGroups.push(group);
	};
	pushGroup(relLiteral);
	if (relStripped !== relLiteral) pushGroup(relStripped);

	// 1) Exact normalised-path match — literal candidates preferred over the
	//    fragment-stripped ones. Newest descriptor wins on a version tie.
	for (const group of candidateGroups) {
		const set = new Set(group);
		const exact = files.filter((file) => {
			const wp = fileWorkspacePath(file);
			return wp && set.has(wp);
		});
		if (exact.length) return pickLatest(exact);
	}

	// 2) Unique-basename fallback — the model named the right file but pointed at
	//    a different directory. Uses the fragment-stripped name so a genuine
	//    `#fragment` doesn't poison the basename. Only resolve when exactly one
	//    file matches, and never across top-level namespaces (inputs/ vs outputs/).
	const rel = relStripped || relLiteral;
	const base = basename(rel);
	const relTop = topSegment(rel);
	if (base) {
		const matches = files.filter((file) => {
			const wp = fileWorkspacePath(file);
			if (!wp || basename(wp) !== base) return false;
			if (relTop && topSegment(wp) !== relTop) return false;
			return true;
		});
		if (matches.length === 1) return matches[0];
	}

	return null;
};

/**
 * The authenticated content URL for a resolved generated file, suitable as an
 * <img>/<audio> src. `apiBase` is WEBUI_API_BASE_URL (`${WEBUI_BASE_URL}/api/v1`),
 * passed in so this module stays free of browser-env imports and unit-testable.
 *
 * When `shareId` is provided (the read-only shared chat page), the URL points at
 * the anonymous-allowed, membership-authorized share route instead of the
 * owner-authenticated one. Omitting `shareId` preserves the original behavior.
 */
export const sandboxFileContentUrl = (
	file: any,
	apiBase: string,
	shareId?: string | null
): string => {
	const id = file?.id ?? file?.file?.id;
	if (!id) return '';
	return shareId
		? `${apiBase}/files/share/${shareId}/${id}/content`
		: `${apiBase}/files/${id}/content`;
};

/**
 * Build a file content URL by id, optionally share-scoped and/or as an
 * attachment download. Mirrors `sandboxFileContentUrl` but takes a bare id.
 * With no options it is identical to the owner-authenticated content route.
 */
export const fileContentUrl = (
	fileId: string,
	apiBase: string,
	opts: { shareId?: string | null; attachment?: boolean } = {}
): string => {
	const { shareId, attachment } = opts;
	const base = shareId
		? `${apiBase}/files/share/${shareId}/${fileId}/content`
		: `${apiBase}/files/${fileId}/content`;
	return attachment ? `${base}?attachment=true` : base;
};

const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico|tiff?|heic|heif)$/i;

/**
 * Whether a resolved sandbox descriptor is an image, so the caller knows to
 * render it inline as an <img> vs. open it in the file-preview panel. Image
 * markdown (`![x](sandbox:…)`) can point at any media; only true images belong
 * in <Image>. Checks declared content type first, then the filename extension.
 */
export const isImageSandboxFile = (file: any): boolean => {
	const ct = String(
		file?.meta?.content_type ?? file?.file?.meta?.content_type ?? file?.content_type ?? ''
	).toLowerCase();
	if (ct.startsWith('image/')) return true;
	const name = String(file?.name ?? file?.file?.meta?.name ?? file?.file?.filename ?? '');
	const wp = String(
		file?.container_workspace?.workspace_path ??
			file?.file?.data?.container_workspace?.workspace_path ??
			''
	);
	return IMAGE_EXT_RE.test(name) || IMAGE_EXT_RE.test(wp);
};
