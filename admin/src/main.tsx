import { h, render } from "preact";

import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("admin: #root element missing from index.html");
}
render(h(App, {}), root);
