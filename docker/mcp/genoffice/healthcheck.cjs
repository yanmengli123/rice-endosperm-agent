const http = require('node:http');
const req = http.get('http://127.0.0.1:3093/health', {timeout: 3000}, res => {
  let text = '';
  res.on('data', chunk => { text += chunk; });
  res.on('end', () => {
    try { process.exit(res.statusCode === 200 && JSON.parse(text).status === 'ok' ? 0 : 1); }
    catch { process.exit(1); }
  });
});
req.on('timeout', () => { req.destroy(); process.exit(1); });
req.on('error', () => process.exit(1));
