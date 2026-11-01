// Notifications pop up as a banner and are gone afterwards. The calendar menu keeps no
// list of app notifications, so it opens and clears instantly however many arrive.
//
// Why: stock GNOME Shell keeps every notification until the user dismisses it and builds a
// widget group per source in the calendar menu. With a few hundred of them, opening the menu
// stalls the shell for seconds and "Clear" animates every group out one by one.
//
// What it does, for notifications of apps (the freedesktop and GTK notification daemons,
// which includes GSConnect):
//  * A sweep destroys every notification that is neither the banner on screen nor waiting in
//    the queue. It runs in an idle callback after anything that can change that: a banner
//    request, the queue, a banner hiding. So a notification lives exactly as long as its
//    banner, and one that never gets a banner (do-not-disturb, low urgency, the app's banners
//    switched off, queue full) goes right after the sender's call returned. Not inside it:
//    the GTK daemon source refuses to destroy itself while its addNotification() runs, and
//    the freedesktop daemon would announce NotificationClosed before replying to Notify.
//  * The calendar menu's message view ignores these sources (media players stay).
//  * On enable the existing backlog is purged in bulk, without the per-group animations.
// Notifications of the Shell itself (Wi-Fi password requests, "Screenshot saved") are never
// destroyed and keep their stock list entry: some stand for a request that is still waiting
// for the user and cancel it when destroyed.
// The lock screen shows no banners: a banner it closes (of any source) is queued again for
// after unlock, and nothing is destroyed while locked. metadata.json keeps the extension on
// while locked, so the unlock does not run the purge.
// disable() restores the stock methods and rebuilds the groups for the sources still alive.
import GLib from 'gi://GLib';
import {Extension, InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageList from 'resource:///org/gnome/shell/ui/messageList.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import {FdoNotificationDaemonSource, GtkNotificationDaemonAppSource} from 'resource:///org/gnome/shell/ui/notificationDaemon.js';

const {NotificationDestroyedReason} = MessageTray;

const isAppSource = source =>
    source instanceof FdoNotificationDaemonSource || source instanceof GtkNotificationDaemonAppSource;

function messageView() {
    return Main.panel.statusArea.dateMenu._messageList._messageView;
}

// The tray decides synchronously inside the banner request whether a notification gets a
// banner: it is either the banner on screen or waiting in the queue.
function hasBanner(notification) {
    const tray = Main.messageTray;
    return tray._notification === notification || tray._notificationQueue.includes(notification);
}

function expireAppNotificationsWithoutBanner() {
    for (const source of Main.messageTray.getSources()) {
        if (!isAppSource(source))
            continue;
        for (const notification of [...source.notifications]) {
            if (!hasBanner(notification))
                notification.destroy(NotificationDestroyedReason.EXPIRED);
        }
    }
}

// Drops the groups of app sources at once. MessageView._removeNotificationSource() would zoom
// each group out first and tear them down in a burst, which is what freezes the shell.
function dropAppNotificationGroups(view) {
    for (const [source, group] of [...view._notificationSourceToGroup]) {
        if (!isAppSource(source))
            continue;
        if (view._expandedGroup === group) {
            view._expandedGroup = null;
            view.notify('expanded-group');
            view._overlay.hide();
            const highlight = view.get_effect('highlight');
            highlight.highlightActor = null;
            // With opacity left at 1 and no actor the fade effect darkens the lower part of the view.
            highlight.opacity = 0;
        }
        if (group.hasUrgent)
            view._nUrgent--;
        group.get_parent().destroy();
        view._notificationSourceToGroup.delete(source);
    }

    // Destroying the items updates view.messages through their destroy handlers but notifies nobody.
    view.notify('empty');
    view.notify('can-clear');
}

export default class QuietNotifications extends Extension {
    enable() {
        this._injections = new InjectionManager();
        this._sweepId = 0;
        this._closedByLock = false;

        try {
            this._install();
        } catch (e) {
            try {
                this.disable();
            } catch (cleanupError) {
                console.error(cleanupError);
            }
            throw e;
        }
    }

    _install() {
        const extension = this;
        const trayPrototype = MessageTray.MessageTray.prototype;

        // The reason is recorded where the hide starts: by the time it completes, the banner
        // may have been leaving for another reason.
        this._injections.overrideMethod(trayPrototype, '_hideNotification', original => {
            return function (animate) {
                extension._closedByLock = !Main.sessionMode.hasNotifications;
                original.call(this, animate);
            };
        });
        this._injections.overrideMethod(trayPrototype, '_hideNotificationCompleted', original => {
            return function () {
                // A banner the lock screen closed was not seen: the stock code must not destroy
                // it even when its app marked it transient, and the tray shows it again later.
                // A withdrawn notification is already destroyed and must not be touched.
                const notification = this._notification;
                const keep = extension._closedByLock && !this._notificationRemoved;
                const wasTransient = keep && notification.isTransient;
                if (keep)
                    notification.isTransient = false;
                original.call(this);
                if (keep) {
                    notification.isTransient = wasTransient;
                    notification.acknowledged = false;
                }
                extension._scheduleSweep();
            };
        });

        const viewPrototype = MessageList.MessageView.prototype;
        this._injections.overrideMethod(viewPrototype, '_addNotificationSource', original => {
            return function (source) {
                if (!isAppSource(source))
                    original.call(this, source);
            };
        });
        this._injections.overrideMethod(viewPrototype, '_removeNotificationSource', original => {
            return function (source) {
                if (this._notificationSourceToGroup.has(source))
                    original.call(this, source);
            };
        });

        const tray = Main.messageTray;
        tray.connectObject(
            'source-added', (_tray, source) => this._watch(source),
            'source-removed', (_tray, source) => source.disconnectObject(this),
            'queue-changed', () => this._scheduleSweep(),
            this);
        Main.sessionMode.connectObject('updated', () => this._scheduleSweep(), this);

        dropAppNotificationGroups(messageView());
        // Nothing is destroyed while locked; _watch() schedules a sweep that waits for the unlock.
        if (Main.sessionMode.hasNotifications)
            expireAppNotificationsWithoutBanner();
        for (const source of tray.getSources())
            this._watch(source);
    }

    // A source that rejoins the tray (its app's notifications were switched off and on) comes
    // with the notifications it collected meanwhile: nothing would ever show or clear them.
    _watch(source) {
        if (!isAppSource(source))
            return;
        source.connectObject('notification-request-banner', () => this._scheduleSweep(), this);
        this._scheduleSweep();
    }

    _scheduleSweep() {
        if (this._sweepId)
            return;
        this._sweepId = GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._sweepId = 0;
            // The lock screen shows no banners; what arrived meanwhile waits for the unlock.
            if (Main.sessionMode.hasNotifications)
                expireAppNotificationsWithoutBanner();
            return GLib.SOURCE_REMOVE;
        });
    }

    disable() {
        this._injections.clear();
        this._injections = null;
        if (this._sweepId)
            GLib.source_remove(this._sweepId);
        this._sweepId = 0;

        Main.sessionMode.disconnectObject(this);
        Main.messageTray.disconnectObject(this);
        const view = messageView();
        for (const source of Main.messageTray.getSources()) {
            source.disconnectObject(this);
            // Give each app source its group back: the stock _removeNotificationSource() throws
            // for a source without one.
            if (isAppSource(source) && !view._notificationSourceToGroup.has(source))
                view._addNotificationSource(source);
        }
    }
}
