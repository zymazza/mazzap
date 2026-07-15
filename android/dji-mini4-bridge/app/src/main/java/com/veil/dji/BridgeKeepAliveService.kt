package com.veil.dji

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.usb.UsbAccessory
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/** Keeps the USB accessory bridge alive when the status Activity is not visible. */
class BridgeKeepAliveService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    private var detachReceiverRegistered = false
    private val usbDetachReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != UsbManager.ACTION_USB_ACCESSORY_DETACHED) return
            val accessory = intent.djiAccessoryExtra() ?: return
            if (!accessory.isDjiLogicLink()) return
            invalidateControlSession("usb_accessory")
            if (!hasAuthorizedDjiAccessory()) stopSelf()
        }
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        ContextCompat.registerReceiver(
            this,
            usbDetachReceiver,
            IntentFilter(UsbManager.ACTION_USB_ACCESSORY_DETACHED),
            ContextCompat.RECEIVER_EXPORTED
        )
        detachReceiverRegistered = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // A sticky service and untimed wake lock must never survive the RC-N2
        // accessory. Re-enumerate on every explicit start and let USB attach or
        // MainActivity start us again after the next authorized connection.
        if (!hasAuthorizedDjiAccessory()) {
            invalidateControlSession("usb_accessory_missing")
            stopSelf(startId)
            return START_NOT_STICKY
        }
        // Re-evaluate after MainActivity's runtime permission result so a
        // service first started for USB can add the location type in place.
        promoteToForeground()
        acquireWakeLock()
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        // The SDK/runtime is Application-owned and can outlive this service.
        // Explicitly invalidate authenticated control before dropping the
        // foreground execution guarantee or wake lock.
        invalidateControlSession("keepalive_service")
        if (detachReceiverRegistered) {
            runCatching { unregisterReceiver(usbDetachReceiver) }
            detachReceiverRegistered = false
        }
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "VEIL DJI bridge",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Keeps the RC-N2 USB bridge and authenticated API available"
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun promoteToForeground() {
        var types = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        } else {
            0
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && hasLocationPermission()) {
            types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        }
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            buildNotification(),
            types
        )
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.ACCESS_FINE_LOCATION
            ) == PackageManager.PERMISSION_GRANTED

    private fun hasAuthorizedDjiAccessory(): Boolean {
        val usbManager = getSystemService(USB_SERVICE) as UsbManager
        return usbManager.accessoryList.orEmpty().any { accessory ->
            accessory.isDjiLogicLink() && usbManager.hasPermission(accessory)
        }
    }

    @SuppressLint("WakelockTimeout")
    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        // This is a continuous USB relay, not a bounded job. Its lifetime is
        // bounded by exact accessory validation and the detach receiver above.
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "VeilDjiBridge:usb-relay")
            .apply {
                setReferenceCounted(false)
                acquire()
            }
    }

    private fun invalidateControlSession(source: String) {
        runCatching {
            (application as? BridgeApplication)
                ?.runtime
                ?.onControlLinkDisconnected(source)
        }
    }

    private fun buildNotification(): Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(android.R.drawable.stat_notify_sdcard_usb)
            .setContentTitle("VEIL DJI bridge active")
            .setContentText("RC-N2 video, telemetry, and control relay")
            .setOngoing(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
    }

    private companion object {
        const val CHANNEL_ID = "veil_dji_bridge"
        const val NOTIFICATION_ID = 4104
        const val DJI_ACCESSORY_MANUFACTURER = "DJI"
        const val DJI_ACCESSORY_MODEL = "com.dji.logiclink"
    }

    private fun UsbAccessory.isDjiLogicLink(): Boolean =
        manufacturer == DJI_ACCESSORY_MANUFACTURER && model == DJI_ACCESSORY_MODEL

    @Suppress("DEPRECATION")
    private fun Intent.djiAccessoryExtra(): UsbAccessory? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getParcelableExtra(UsbManager.EXTRA_ACCESSORY, UsbAccessory::class.java)
        } else {
            getParcelableExtra(UsbManager.EXTRA_ACCESSORY)
        }
}
