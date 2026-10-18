// AgentTab API reference: Swagger UI, self-hosted, reading our own /openapi.json. No validator badge (it would call
// validator.swagger.io), no topbar, no outside requests.
window.addEventListener('load', () => {
  window.ui = SwaggerUIBundle({url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true, validatorUrl: null,
    presets: [SwaggerUIBundle.presets.apis], layout: 'BaseLayout', docExpansion: 'list', defaultModelsExpandDepth: 0,
    tryItOutEnabled: false, syntaxHighlight: false});
});
