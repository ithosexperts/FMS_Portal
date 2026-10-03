/* API Explorer - "Test" button wiring (posts to /developer/apis/test). */

async function testEndpoint(button, endpointKey) {
  const card = button.closest("[data-endpoint-card]");
  const output = card.querySelector("[data-endpoint-output]");
  const pathParamsRaw = card.querySelector("[data-path-params]")?.value || "{}";
  const queryParamsRaw = card.querySelector("[data-query-params]")?.value || "{}";

  let pathParams, queryParams;
  try {
    pathParams = JSON.parse(pathParamsRaw);
    queryParams = JSON.parse(queryParamsRaw);
  } catch {
    output.textContent = "Invalid JSON in path/query params.";
    return;
  }

  button.disabled = true;
  output.textContent = "Loading...";

  try {
    const response = await fetch("/developer/apis/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint_key: endpointKey, path_params: pathParams, query_params: queryParams }),
    });
    const json = await response.json();
    output.textContent = JSON.stringify(json, null, 2);
  } catch (err) {
    output.textContent = `Request failed: ${err}`;
  } finally {
    button.disabled = false;
  }
}
