var fs = require('fs');
var css = fs.readFileSync('C:/Users/User/.codexthemes/themes/lazy-cow-pasture/theme.css', 'utf8');
var lines = css.split('\n').map(function(l) { return JSON.stringify(l); });
var body = 'var css = [' + lines.join(',') + '].join(String.fromCharCode(10));\n'
  + 'var s = document.createElement("style");\n'
  + 's.textContent = css;\n'
  + 's.setAttribute("data-theme-id", "lazy-cow-pasture");\n'
  + 'document.head.prepend(s);\n'
  + 'console.log("%c\\uD83D\\uDC04 Pasture theme applied", "color:#6B8F3C;font-size:14px");';
fs.writeFileSync('D:/Projects/Own Projects/Aegis/pasture-inject.js',
  '// Lazy Cow Pasture -- self-inject\n'
  + '// Paste into Codex DevTools Console (F12)\n'
  + '// To remove: document.querySelector("style[data-theme-id]").remove()\n\n'
  + body);
console.log('OK, size: ' + fs.statSync('D:/Projects/Own Projects/Aegis/pasture-inject.js').size + ' bytes');
