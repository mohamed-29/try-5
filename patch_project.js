const fs = require('fs');
const path = require('path');

const SOURCE_DIR = 'IvendApp_Source';
const TARGET_DIR = 'IvendApp';

function copyFile(src, dest) {
    const destDir = path.dirname(dest);
    if (!fs.existsSync(destDir)) {
        fs.mkdirSync(destDir, { recursive: true });
    }
    fs.copyFileSync(src, dest);
    console.log(`Copied: ${src} -> ${dest}`);
}

function copyDir(src, dest) {
    if (!fs.existsSync(dest)) {
        fs.mkdirSync(dest, { recursive: true });
    }
    const entries = fs.readdirSync(src, { withFileTypes: true });
    for (const entry of entries) {
        const srcPath = path.join(src, entry.name);
        const destPath = path.join(dest, entry.name);
        if (entry.isDirectory()) {
            copyDir(srcPath, destPath);
        } else {
            fs.copyFileSync(srcPath, destPath);
            console.log(`Copied: ${srcPath} -> ${destPath}`);
        }
    }
}

async function main() {
    if (!fs.existsSync(SOURCE_DIR)) {
        console.error(`Error: Source directory '${SOURCE_DIR}' not found. Please rename your existing 'IvendApp' folder to 'IvendApp_Source'.`);
        return;
    }
    if (!fs.existsSync(TARGET_DIR)) {
        console.error(`Error: Target directory '${TARGET_DIR}' not found. Please run 'npx react-native init IvendApp' first.`);
        return;
    }

    console.log('Starting patch process...');

    // 1. Copy src folder
    copyDir(path.join(SOURCE_DIR, 'src'), path.join(TARGET_DIR, 'src'));

    // 2. Copy App.tsx
    copyFile(path.join(SOURCE_DIR, 'App.tsx'), path.join(TARGET_DIR, 'App.tsx'));

    // 3. Copy Native Modules
    const androidSrc = path.join(SOURCE_DIR, 'android/app/src/main/java/com/ivendapp');
    const androidDest = path.join(TARGET_DIR, 'android/app/src/main/java/com/ivendapp');

    if (fs.existsSync(androidSrc)) {
        copyFile(path.join(androidSrc, 'GeideaModule.java'), path.join(androidDest, 'GeideaModule.java'));
        copyFile(path.join(androidSrc, 'GeideaPackage.java'), path.join(androidDest, 'GeideaPackage.java'));
    } else {
        console.warn('Warning: Native module source files not found.');
    }

    // 4. Patch MainApplication.java
    const mainAppPath = path.join(TARGET_DIR, 'android/app/src/main/java/com/ivendapp/MainApplication.java');
    if (fs.existsSync(mainAppPath)) {
        let content = fs.readFileSync(mainAppPath, 'utf8');
        if (!content.includes('new GeideaPackage()')) {
            // Add import if missing (simple check)
            if (!content.includes('import com.ivendapp.GeideaPackage;')) {
                // It's in the same package, so import might not be needed, but good practice if structure differs.
                // Actually, if they are in the same package, no import needed.
            }

            // Add package to getPackages()
            // Look for "List<ReactPackage> packages = new PackageList(this).getPackages();"
            const searchStr = 'List<ReactPackage> packages = new PackageList(this).getPackages();';
            const replaceStr = 'List<ReactPackage> packages = new PackageList(this).getPackages();\n          packages.add(new GeideaPackage()); // Added by Ivend Patch';

            if (content.includes(searchStr)) {
                content = content.replace(searchStr, replaceStr);
                fs.writeFileSync(mainAppPath, content);
                console.log('Patched: MainApplication.java');
            } else {
                console.warn('Warning: Could not auto-patch MainApplication.java. Please add GeideaPackage manually.');
            }
        }
    }

    // 5. Patch build.gradle (app level)
    const buildGradlePath = path.join(TARGET_DIR, 'android/app/build.gradle');
    if (fs.existsSync(buildGradlePath)) {
        let content = fs.readFileSync(buildGradlePath, 'utf8');
        if (!content.includes('net.geidea.sdk:pos-connect-sdk-egp')) {
            const depStr = 'dependencies {';
            const newDep = 'dependencies {\n    implementation \'net.geidea.sdk:pos-connect-sdk-egp:1.0.4\'';
            content = content.replace(depStr, newDep);
            fs.writeFileSync(buildGradlePath, content);
            console.log('Patched: android/app/build.gradle');
        }
    }

    // 6. Install dependencies
    console.log('Installing dependencies...');
    // We need to install react-native-svg and others if used
    // But since I cannot run npm easily, I will just log instructions.
    console.log('Patch complete!');
    console.log('Please run: cd IvendApp && npm install react-native-svg');
}

main();
