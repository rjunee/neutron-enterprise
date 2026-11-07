import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

// THIN-CLIENT GUARDIAN (C6/C7): all network I/O must go through src/api/. Any
// fetch()/EventSource/WebSocket/XMLHttpRequest outside src/api/ is a lint error,
// so the console can never open a second, ungoverned channel to any data plane.
const networkPrimitivesBanned = {
  rules: {
    "no-restricted-syntax": [
      "error",
      {
        selector: "CallExpression[callee.name='fetch']",
        message: "All network I/O must go through src/api/ — thin-client invariant C6/C7.",
      },
      {
        selector: "NewExpression[callee.name='EventSource']",
        message: "All network I/O must go through src/api/ — thin-client invariant C6/C7.",
      },
      {
        selector: "NewExpression[callee.name='WebSocket']",
        message: "All network I/O must go through src/api/ — thin-client invariant C6/C7.",
      },
      {
        selector: "NewExpression[callee.name='XMLHttpRequest']",
        message: "All network I/O must go through src/api/ — thin-client invariant C6/C7.",
      },
    ],
  },
};

export default tseslint.config(
  // src/vendor is vendored upstream code (d3-force + siblings) + our hand-written
  // .d.ts over it — not our lint surface (kept byte-comparable to upstream).
  { ignores: ["dist", "coverage", "node_modules", "src/vendor"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Allow intentionally-unused, underscore-prefixed args (e.g. typed fetch
      // mock signatures whose params exist only to shape the tuple).
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      // The guardian applies everywhere by default; src/api/** re-enables the
      // primitives via the override below.
      ...networkPrimitivesBanned.rules,
    },
  },
  {
    // src/api/ is the ONE sanctioned network boundary — the primitives are
    // permitted here (and only here). Config reads env only in src/api/config.ts.
    files: ["src/api/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": "off",
    },
  },
  {
    // Test/config files may use node globals.
    files: ["**/*.test.{ts,tsx}", "src/test/**/*.{ts,tsx}", "vite.config.ts"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      // Test helpers legitimately co-export components and fixtures; fast-refresh
      // does not apply to the test tree.
      "react-refresh/only-export-components": "off",
    },
  },
);
