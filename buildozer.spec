[app]
title = Waterpolo Tracker
package.name = waterpolo
package.domain = org.waterpolo.tracker

source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas

version = 0.1
requirements = python3,kivy==2.1.0

[buildozer]
log_level = 1

[android]
android.api = 30
android.minapi = 21
android.ndk = 23b
android.sdk = 30
android.accept_sdk_license = true
