import coreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

const eslintConfig = [...coreWebVitals, ...nextTypeScript];

export default eslintConfig;
