// AgentTab API reference: Swagger UI, self-hosted, reading our own /openapi.json. No validator badge (it would call
// validator.swagger.io), no topbar, no outside requests.
// If the Swagger UI bundle did not load (blocked by its SRI check, a network error, a script blocker), the page says so
// and links the raw OpenAPI document instead of staying blank under the header.
function docsFallback(root) {
  root.replaceChildren();
  const box = document.createElement('div');
  box.className = 'docs-fallback'; box.setAttribute('role', 'alert');
  const p1 = document.createElement('p');
  p1.textContent = "The interactive reference didn't load in this browser.";
  const p2 = document.createElement('p');
  p2.append('The full API description is still here: ');
  const a = document.createElement('a');
  a.href = '/openapi.json'; a.textContent = '/openapi.json';
  p2.append(a, '.');
  box.append(p1, p2);
  root.append(box);
}

window.addEventListener('load', () => {
  const root = document.getElementById('swagger-ui');
  if (typeof window.SwaggerUIBundle !== 'function') { docsFallback(root); return; }
  try {
    window.ui = SwaggerUIBundle({url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true, validatorUrl: null,
      presets: [SwaggerUIBundle.presets.apis], layout: 'BaseLayout', docExpansion: 'list', defaultModelsExpandDepth: 0,
      tryItOutEnabled: false, syntaxHighlight: false});
  } catch (e) {
    docsFallback(root);
  }
});
