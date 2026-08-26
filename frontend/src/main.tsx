import { createRoot } from "react-dom/client";
import App from "./App";
import { initTheme } from "./theme";
// Before the sheet that uses them, so the @font-face rules are in place when
// the first rule referencing a family is applied.
import "./fonts.css";
import "./styles.css";

initTheme();

createRoot(document.getElementById("root")!).render(<App />);
