// Switches the input source to the "us" layout every time the overview
// (Super key / Activities / app grid search) starts opening, so typed
// search queries are always in English.
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {getInputSourceManager} from 'resource:///org/gnome/shell/ui/status/keyboard.js';

const TARGET_LAYOUT = 'us';

export default class EnglishInOverview extends Extension {
    enable() {
        this._showingId = Main.overview.connect('showing', () => {
            const sources = Object.values(getInputSourceManager().inputSources);
            sources.find(s => s.id === TARGET_LAYOUT)?.activate(true);
        });
    }

    disable() {
        Main.overview.disconnect(this._showingId);
        this._showingId = null;
    }
}
