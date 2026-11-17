/* Preserve existing precedence during migration and prevent new priority
   escalation. Each cabinet selector belongs to one source module. */
const fs = require('node:fs');
const path = require('node:path');
const postcss = require('postcss');
const selectorParser = require('postcss-selector-parser');

function inventory(source) {
  const owners = new Map();
  const exceptions = {};
  const errors = [];

  function scan(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, item.name);
      if (item.isDirectory()) {
        scan(file);
        continue;
      }
      if (!file.endsWith('.css')) continue;

      const relative = path.relative(source, file).split(path.sep).join('/');
      const root = postcss.parse(fs.readFileSync(file, 'utf8'), { from: file });
      root.walkAtRules(rule => {
        if (rule.name === 'import') {
          errors.push(`${relative}: use the entry manifest instead of @import`);
        }
      });
      root.walkRules(rule => {
        if (rule.parent.name?.endsWith('keyframes')) return;

        const ast = selectorParser().astSync(rule.selector);
        let repeated = false;
        ast.walk(node => {
          if (node.type !== 'selector') return;
          const compound = new Set();
          for (const part of node.nodes) {
            if (part.type === 'combinator') {
              compound.clear();
              continue;
            }
            if (!['class', 'id'].includes(part.type)) continue;
            const key = part.type + ':' + part.value;
            if (compound.has(key)) repeated = true;
            compound.add(key);
          }
        });

        // Check each branch: grouping .key,.other must not bypass .key's owner.
        // Standalone apps intentionally have independent resets and are never
        // loaded together, so ownership applies to the cabinet modules only.
        if (relative.startsWith('cabinet/')) {
          for (const branch of ast.nodes) {
            const selector = selectorParser().processSync(branch.toString(), {
              lossless: false,
            });
            const previous = owners.get(selector);
            if (previous && previous !== relative) {
              errors.push(`${selector}: owned by both ${previous} and ${relative}`);
            }
            owners.set(selector, relative);
          }
        }

        const selector = selectorParser().processSync(rule.selector, {
          lossless: false,
        });
        const important = rule.nodes.filter(
          node => node.type === 'decl' && node.important,
        ).length;
        if (important || repeated) {
          const key = relative + '|' + selector;
          exceptions[key] ??= { important: 0, repeated };
          exceptions[key].important += important;
          exceptions[key].repeated ||= repeated;
        }
      });
    }
  }

  scan(source);
  return { exceptions, errors };
}

function validate(source) {
  const result = inventory(source);
  const allowed = JSON.parse(
    fs.readFileSync(path.join(source, 'compatibility.json'), 'utf8'),
  );
  for (const [key, item] of Object.entries(result.exceptions)) {
    const limit = allowed[key];
    if (!limit || item.important > limit.important || (item.repeated && !limit.repeated)) {
      result.errors.push('New priority override: ' + key);
    }
  }
  if (result.errors.length) throw Error(result.errors.join('\n'));
  return Object.keys(result.exceptions).length;
}

module.exports = { inventory, validate };
