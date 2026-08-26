import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

import { compile, preprocess } from 'svelte/compiler';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import ts from 'typescript';

const sourceRoot = path.resolve('src');
const preprocessor = vitePreprocess();

const collectSvelteFiles = async (directory) => {
	const entries = await fs.readdir(directory, { withFileTypes: true });
	const files = await Promise.all(
		entries.map((entry) => {
			const target = path.join(directory, entry.name);
			return entry.isDirectory()
				? collectSvelteFiles(target)
				: Promise.resolve(entry.name.endsWith('.svelte') ? [target] : []);
		})
	);
	return files.flat();
};

const stripComments = (source) =>
	source
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/^\s*\/\/.*$/gm, '');

const forbiddenSyntax = [
	['legacy props', /\bexport\s+let\b/],
	['legacy reactive statement', /^\s*\$:/m],
	['legacy event directive', /\son:[A-Za-z_][A-Za-z0-9_-]*=/],
	['legacy slot element', /<slot(?:\s|>|\/)/],
	['legacy helper import', /from\s+['"]svelte\/legacy['"]/],
	['legacy event dispatcher', /\bcreateEventDispatcher\b/],
	['legacy internal props API', /\$\$(?:props|restProps|slots)\b/],
	['legacy dynamic component', /<svelte:(?:component|self)\b/],
	[
		'structuredClone can throw on rune proxies; use $state.snapshot or a non-component utility',
		/\bstructuredClone\s*\(/
	]
];

const getBindingNames = (name, names = []) => {
	if (ts.isIdentifier(name)) {
		names.push(name.text);
	} else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
		for (const element of name.elements) {
			if (ts.isBindingElement(element)) getBindingNames(element.name, names);
		}
	}
	return names;
};

const containsPropsCall = (node) => {
	let found = false;
	const visit = (child) => {
		if (
			ts.isCallExpression(child) &&
			ts.isIdentifier(child.expression) &&
			child.expression.text === '$props'
		) {
			found = true;
			return;
		}
		ts.forEachChild(child, visit);
	};
	visit(node);
	return found;
};

// Collect only references evaluated by a top-level initializer. References
// inside a function/closure are safe because they run after component setup.
const collectImmediateReferences = (node, names) => {
	const references = new Set();
	const visit = (child) => {
		if (
			ts.isArrowFunction(child) ||
			ts.isFunctionExpression(child) ||
			ts.isFunctionDeclaration(child)
		) {
			return;
		}
		if (ts.isIdentifier(child) && names.has(child.text)) {
			const parent = child.parent;
			const isPropertyName =
				(ts.isPropertyAccessExpression(parent) && parent.name === child) ||
				(ts.isPropertyAssignment(parent) && parent.name === child) ||
				(ts.isMethodDeclaration(parent) && parent.name === child);
			if (!isPropertyName) references.add(child.text);
		}
		ts.forEachChild(child, visit);
	};
	visit(node);
	return [...references];
};

const inspectInstanceScript = (source, filename) => {
	const issues = [];
	const scriptPattern = /<script([^>]*)>([\s\S]*?)<\/script>/g;

	for (const match of source.matchAll(scriptPattern)) {
		if (/(?:\bmodule\b|context\s*=)/.test(match[1])) continue;

		const script = match[2];
		const sourceFile = ts.createSourceFile(
			filename,
			script,
			ts.ScriptTarget.Latest,
			true,
			ts.ScriptKind.TS
		);
		const scriptLineOffset = source.slice(0, match.index).split('\n').length - 1;
		let propsStatementIndex = -1;
		let propNames = [];

		for (let index = 0; index < sourceFile.statements.length; index += 1) {
			const statement = sourceFile.statements[index];
			if (!ts.isVariableStatement(statement)) continue;
			for (const declaration of statement.declarationList.declarations) {
				if (declaration.initializer && containsPropsCall(declaration.initializer)) {
					propsStatementIndex = index;
					propNames = getBindingNames(declaration.name);
					break;
				}
			}
			if (propsStatementIndex >= 0) break;
		}

		if (propsStatementIndex >= 0) {
			const names = new Set(propNames);
			for (let index = 0; index < propsStatementIndex; index += 1) {
				const statement = sourceFile.statements[index];
				const initializers = ts.isVariableStatement(statement)
					? statement.declarationList.declarations
							.map((declaration) => declaration.initializer)
							.filter(Boolean)
					: ts.isExpressionStatement(statement)
						? [statement.expression]
						: [];
				for (const initializer of initializers) {
					const references = collectImmediateReferences(initializer, names);
					if (references.length > 0) {
						const line =
							sourceFile.getLineAndCharacterOfPosition(initializer.getStart()).line +
							1 +
							scriptLineOffset;
						issues.push(
							`${path.relative(process.cwd(), filename)}:${line}: props used before $props() (${references.join(', ')})`
						);
					}
				}
			}
		}

		// A direct async route loader called from an effect implicitly tracks
		// every state read before its first await. If that loader also redirects
		// on failure, an unrelated state change can consume one-shot preload data
		// and bounce the route home. Require an explicit untrack boundary.
		const redirectingAsyncFunctions = new Set();
		for (const statement of sourceFile.statements) {
			let name = null;
			let fn = null;
			if (ts.isFunctionDeclaration(statement) && statement.name) {
				name = statement.name.text;
				fn = statement;
			} else if (ts.isVariableStatement(statement)) {
				for (const declaration of statement.declarationList.declarations) {
					if (
						ts.isIdentifier(declaration.name) &&
						declaration.initializer &&
						(ts.isArrowFunction(declaration.initializer) ||
							ts.isFunctionExpression(declaration.initializer))
					) {
						name = declaration.name.text;
						fn = declaration.initializer;
					}
				}
			}
			if (
				name &&
				fn?.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.AsyncKeyword) &&
				/\bgoto\s*\(/.test(fn.getText(sourceFile))
			) {
				redirectingAsyncFunctions.add(name);
			}
		}

		const visitEffects = (node) => {
			if (
				ts.isCallExpression(node) &&
				ts.isIdentifier(node.expression) &&
				node.expression.text === '$effect' &&
				node.arguments.length > 0
			) {
				const callback = node.arguments[0];
				const directCalls = new Set();
				const visitEffectBody = (child, isRoot = false) => {
					if (
						!isRoot &&
						(ts.isArrowFunction(child) ||
							ts.isFunctionExpression(child) ||
							ts.isFunctionDeclaration(child))
					) {
						return;
					}
					if (ts.isCallExpression(child) && ts.isIdentifier(child.expression)) {
						directCalls.add(child.expression.text);
					}
					ts.forEachChild(child, (grandchild) => visitEffectBody(grandchild, false));
				};
				visitEffectBody(callback, true);

				for (const call of directCalls) {
					if (redirectingAsyncFunctions.has(call)) {
						const line =
							sourceFile.getLineAndCharacterOfPosition(node.getStart()).line +
							1 +
							scriptLineOffset;
						issues.push(
							`${path.relative(process.cwd(), filename)}:${line}: redirecting async loader ${call}() must be called inside untrack()`
						);
					}
				}
			}
			ts.forEachChild(node, visitEffects);
		};
		visitEffects(sourceFile);
	}

	return issues;
};

const files = await collectSvelteFiles(sourceRoot);
const failures = [];

for (const filename of files) {
	const source = await fs.readFile(filename, 'utf8');
	const uncommented = stripComments(source);

	for (const [label, pattern] of forbiddenSyntax) {
		if (pattern.test(uncommented)) {
			failures.push(`${path.relative(process.cwd(), filename)}: ${label}`);
		}
	}
	failures.push(...inspectInstanceScript(source, filename));

	try {
		const processed = await preprocess(source, preprocessor, { filename });
		const compiled = compile(processed.code, {
			filename,
			generate: 'client',
			runes: true
		});
		for (const warning of compiled.warnings ?? []) {
			if (warning.code === 'state_referenced_locally') {
				const line = warning.start?.line ?? '?';
				failures.push(
					`${path.relative(process.cwd(), filename)}:${line}: ${warning.message}`
				);
			}
		}
	} catch (error) {
		failures.push(
			`${path.relative(process.cwd(), filename)}: ${error?.message ?? String(error)}`
		);
	}
}

if (failures.length) {
	console.error(`Runes validation failed with ${failures.length} issue(s):`);
	for (const failure of failures) console.error(`- ${failure}`);
	process.exit(1);
}

console.log(`Runes validation passed for ${files.length} Svelte components.`);
