import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const reviewFunction = source.slice(source.indexOf("function canReviewProduct(product)"),
  source.indexOf("async function verifySaasIntegration(product)"));

test("review controls enforce business permissions and preserve other stages", () => {
  const allowed = {
    pending_review: ["super_admin", "product_manager"],
    quality_review: ["super_admin", "quality_reviewer"],
    security_review: ["super_admin", "security_compliance"],
    operation_review: ["super_admin", "platform_operator"],
    draft: [], rejected: [], published: [],
  };
  for (const [status, roles] of Object.entries(allowed)) {
    for (const role of ["super_admin", "product_manager", "business_reviewer",
      "quality_reviewer", "security_compliance", "platform_operator", ""]) {
      const context = vm.createContext({ user: { value: { platform_role: role } } });
      vm.runInContext(reviewFunction, context);
      assert.equal(context.canReviewProduct({ status }), roles.includes(role), `${status}: ${role}`);
    }
  }
});
