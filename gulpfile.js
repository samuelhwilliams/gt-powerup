'use strict';

// Import required modules
// -----------------------
const gulp = require('gulp'),
    sass = require('gulp-sass'),
    concat = require('gulp-concat'),
    sourcemaps = require('gulp-sourcemaps');

// Copy assets from vendors to the application's src and static folders
// --------------------------------------------------------------------
gulp.task('copy-govuk-frontend-js', function () {
    return gulp.src(['./node_modules/govuk-frontend/all.js'])
    .pipe(concat('govuk-frontend-all.js', { newline: ';' } ))
    .pipe(gulp.dest('./app/src/js/vendor'))
})

gulp.task('copy-icons', function () {
    return gulp.src(['./node_modules/govuk-frontend/assets/images/icon-*.png'])
    .pipe(gulp.dest('./app/assets/images'))
})

// Compile src files down to static assets
// ---------------------------------------
gulp.task('compile-css', function () {
  return gulp.src(['./app/src/scss/*.scss'])
    .pipe(sourcemaps.init())
    .pipe(sass({outputStyle: 'compressed'}).on('error', sass.logError))
    .pipe(sourcemaps.write('.', {sourceRoot: '../src'}))
    .pipe(gulp.dest('./app/assets/css'))
});

gulp.task('compile-js', function() {
    return gulp.src(['./app/src/js/vendor/**/*.js', './app/src/js/*.js'])
    .pipe(sourcemaps.init())
    .pipe(concat('application.js', { newline: ';' } ))
    .pipe(sourcemaps.write('.', {sourceRoot: '../src'}))
    .pipe(gulp.dest('./app/assets/js'))
});

// Main pipeline chained tasks
// ---------------------------
gulp.task('vendor', gulp.parallel('copy-govuk-frontend-js', 'copy-icons'));

gulp.task('compile', gulp.parallel('compile-js', 'compile-css'));

// Main entrypoint
// ---------------
gulp.task('default', gulp.series('vendor', 'compile'));

// Dev task to watch src files for changes and recompile
// -----------------------------------------------------
gulp.task('watch', function () {
  gulp.watch(['./app/src/js/!(vendor)/*.js', './app/src/js/*.js', './app/src/scss/*.scss', './app/src/scss/**/*.scss'], gulp.series('default'));
});