package com.veil.dji

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.hardware.usb.UsbManager
import android.widget.TextView
import androidx.core.content.ContextCompat

class MainActivity : Activity() {
    private lateinit var status: TextView
    private val statusRefresh = object : Runnable {
        override fun run() {
            val app = application as BridgeApplication
            if (BridgeState.productConnected.get()) {
                app.runtime.videoRelay.ensureMainCameraObserver()
            }
            status.text = "${BridgeState.toJson().toString(2)}\nHTTP port: 8765\nAuthentication: configured"
            status.postDelayed(this, 500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        status = TextView(this).apply {
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.rgb(20, 20, 20))
            textSize = 12f
            setPadding(16, 12, 16, 12)
        }
        setContentView(status)

        syncKeepAliveWithAccessory()
        requestLocationPermissionsIfNeeded()

        status.post(statusRefresh)
    }

    override fun onResume() {
        super.onResume()
        syncKeepAliveWithAccessory()
        (application as? BridgeApplication)?.runtime?.refreshAndroidLocationReadiness()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == LOCATION_PERMISSION_REQUEST) {
            syncKeepAliveWithAccessory()
            (application as? BridgeApplication)?.runtime?.refreshAndroidLocationReadiness()
        }
    }

    override fun onDestroy() {
        status.removeCallbacks(statusRefresh)
        super.onDestroy()
    }

    private fun requestLocationPermissionsIfNeeded() {
        val coarseGranted = checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        val fineGranted = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        if (!coarseGranted || !fineGranted) {
            requestPermissions(
                arrayOf(
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                    Manifest.permission.ACCESS_FINE_LOCATION
                ),
                LOCATION_PERMISSION_REQUEST
            )
        }
    }

    private fun syncKeepAliveWithAccessory() {
        val usbManager = getSystemService(USB_SERVICE) as UsbManager
        val authorizedDjiAccessory = usbManager.accessoryList.orEmpty().any { accessory ->
            accessory.manufacturer == "DJI" &&
                accessory.model == "com.dji.logiclink" &&
                usbManager.hasPermission(accessory)
        }
        val serviceIntent = Intent(this, BridgeKeepAliveService::class.java)
        if (authorizedDjiAccessory) {
            ContextCompat.startForegroundService(this, serviceIntent)
        } else {
            stopService(serviceIntent)
        }
    }

    private companion object {
        const val LOCATION_PERMISSION_REQUEST = 41
    }
}
