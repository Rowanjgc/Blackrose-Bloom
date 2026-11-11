// Check against the pinned declarations, without loading a scene or its config.
import ts from 'typescript';
import {fileURLToPath} from 'node:url';

const filename = fileURLToPath(new URL('./scene.tsx', import.meta.url));
const allowed = new Set(['@motion-canvas/core', '@motion-canvas/2d',
  '@motion-canvas/2d/lib/jsx-runtime']);

// Motion Canvas delegates color parsing to chroma-js, whose color-name parser
// does not accept CSS/SVG paint keywords. Convert only unambiguous literals;
// leave expressions and every other color untouched.
export function normalizeSource(source) {
  const file = ts.createSourceFile('scene.tsx', source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  let changed = false;
  const paint = (property, initializer) => {
    if (!['fill', 'stroke'].includes(property) || !initializer) return undefined;
    const value = ts.isJsxExpression(initializer) ? initializer.expression : initializer;
    if (!value || !(ts.isStringLiteralLike(value))) return undefined;
    const keyword = value.text.toLowerCase();
    if (keyword === 'none' || keyword === 'null') return {kind: 'empty'};
    if (keyword === 'transparent') return {kind: 'color', value: 'rgba(0, 0, 0, 0)'};
    return undefined;
  };
  const transformer = context => {
    const visit = node => {
      if (ts.isJsxAttribute(node)) {
        const replacement = paint(node.name.getText(file), node.initializer);
        if (replacement) {
          changed = true;
          const initializer = replacement.kind === 'empty'
            ? context.factory.createJsxExpression(undefined, context.factory.createNull())
            : context.factory.createJsxExpression(undefined, context.factory.createStringLiteral(replacement.value));
          return context.factory.updateJsxAttribute(node, node.name, initializer);
        }
      }
      if (ts.isPropertyAssignment(node)) {
        const name = node.name;
        const property = ts.isIdentifier(name) || ts.isStringLiteralLike(name) ? name.text : '';
        const replacement = paint(property, node.initializer);
        if (replacement) {
          changed = true;
          const initializer = replacement.kind === 'empty'
            ? context.factory.createNull()
            : context.factory.createStringLiteral(replacement.value);
          return context.factory.updatePropertyAssignment(node, name, initializer);
        }
      }
      return ts.visitEachChild(node, visit, context);
    };
    return root => ts.visitNode(root, visit);
  };
  const transformed = ts.transform(file, [transformer]);
  try {
    return changed ? ts.createPrinter().printFile(transformed.transformed[0]) : source;
  } finally {
    transformed.dispose();
  }
}

export function checkSource(source) {
  const options = {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    jsx: ts.JsxEmit.ReactJSX, jsxImportSource: '@motion-canvas/2d/lib',
    noEmit: true, skipLibCheck: true, types: [], noImplicitAny: true, strictNullChecks: true,
    experimentalDecorators: true, allowSyntheticDefaultImports: true,
  };
  const file = ts.createSourceFile(filename, source, options.target, true, ts.ScriptKind.TSX);
  const errors = [];
  const report = (node, message) => {
    const {line, character} = file.getLineAndCharacterOfPosition(node.getStart(file));
    errors.push(`scene.tsx:${line + 1}:${character + 1}: ${message}`);
  };
  if (file.referencedFiles.length || file.typeReferenceDirectives.length || file.libReferenceDirectives.length) {
    report(file, 'Reference directives are unavailable. Use the installed Motion Canvas imports.');
  }
  let hasDefault = false;
  const visit = node => {
    if (ts.isExportAssignment(node) && !node.isExportEquals) hasDefault = true;
    if (node.modifiers?.some(m => m.kind === ts.SyntaxKind.DefaultKeyword)) hasDefault = true;
    if (ts.isExportDeclaration(node) && node.exportClause && ts.isNamedExports(node.exportClause)) {
      hasDefault ||= node.exportClause.elements.some(e => e.name.text === 'default');
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  if (!hasDefault) report(file, 'Export default makeScene2D(function* (view) {...}).');
  if (errors.length) return errors;

  const host = ts.createCompilerHost(options);
  const getSourceFile = host.getSourceFile.bind(host);
  host.getSourceFile = (path, ...args) => path === filename ? file : getSourceFile(path, ...args);
  // Generated type imports cannot resolve arbitrary files, even when erased by esbuild.
  host.resolveModuleNames = (names, containingFile) => names.map(name => {
    if (containingFile === filename && !allowed.has(name)) return undefined;
    return ts.resolveModuleName(name, containingFile, options, ts.sys).resolvedModule;
  });
  const program = ts.createProgram([filename], options, host);
  for (const diagnostic of ts.getPreEmitDiagnostics(program)) {
    if (diagnostic.file?.fileName !== filename) continue;
    const {line, character} = file.getLineAndCharacterOfPosition(diagnostic.start || 0);
    errors.push(`scene.tsx:${line + 1}:${character + 1}: ${ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n')}`);
    if (errors.length === 8) break;
  }
  if (errors.length) return errors;

  // A property tween or timeline call in statement position returns a generator
  // that Motion Canvas never schedules. spawn() is the intentional exception: it
  // starts work immediately. Catch this before playback so an invisible tween
  // cannot pass as a finished animation.
  const checker = program.getTypeChecker();
  const calleeName = expression => {
    if (ts.isIdentifier(expression)) return expression.text;
    if (ts.isPropertyAccessExpression(expression)) return expression.name.text;
    return '';
  };
  const visitStatements = node => {
    if (errors.length === 8) return;
    if (ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)) {
      const call = node.expression;
      const type = checker.typeToString(checker.getTypeAtLocation(call));
      const generator = type.startsWith('SignalGenerator<') || type === 'ThreadGenerator'
        || type.startsWith('ThreadGenerator<');
      if (generator && calleeName(call.expression) !== 'spawn') {
        report(call, 'Animation call returns a generator but is not scheduled; '
          + 'use yield* or include it in a yielded all(...).');
      }
    }
    ts.forEachChild(node, visitStatements);
  };
  visitStatements(file);
  return errors;
}
