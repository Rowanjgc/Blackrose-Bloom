# Blackrose design studies

Open `index.html` in a browser. No server or dependencies are required. Use the
three direction buttons and the Conversation / Library controls to compare.
The original application is unchanged.

| Direction | Composition | Tradeoff |
| --- | --- | --- |
| Monograph | A centered reading column, fine rules, square controls, generous leading | Sources open on demand |
| Research Folio | Compact left navigation, annotated prose, source notes in the right margin | More information competes with the conversation |
| Reading Room | Horizontal navigation, softly framed reading sheet, rounded utilities | Recent conversations are less prominent |

The purpose is to compare scholarly visual languages for Blackrose's existing
conversation and local-source workflows. All studies use the shipped Libron
regular, bold, and italic fonts and Gothic Bloom's existing color values.
Cherry identifies the brand, rose marks selection and sending, gold marks rare
source details. Layout, spacing, typography, and geometry create the differences.

On narrow screens, the reading column fills the width, marginal notes become an
inline source reference, and library metadata moves beneath each title. The
preview direction and screen controls remain available. All controls use native
button/input/dialog behavior with visible keyboard focus and immediate press
feedback; reduced motion suppresses transitions.

Content and model status are illustrative. Source 1 quotes the repository's
CONTEXT.md. Search filters only the three sample documents. Sending retains the
preview draft and explains that no model request was made. Utility buttons show
explanatory dialogs. These are design studies, not alternate app implementations.

Verification: rendered all three directions at 1440px and 390px for both screens;
checked font loading, horizontal overflow, direction/screen switching, source
modal opening and Escape dismissal, draft preservation, library filtering and
its no-results state, and JavaScript errors. Visually inspected all desktop
conversation layouts, the Monograph mobile conversation, and desktop Reading
Room / mobile Folio libraries. PNGs in this directory capture the rendered views.
