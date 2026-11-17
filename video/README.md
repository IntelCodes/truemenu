# Demo Day video (69 s, 1920x1080, no sound)

Rebuild from a running local booth (port 8099, presenter token `localtoken`):

    node video/capture.js "$PWD/video"       # writes video/frames/*.jpg from the live booth page
    # then encode each still with a short fade and join them (see the commit message of this folder)

Every scene is a screenshot of the real booth page against the real Kendall Square menu data.
