const clampCoordinate = (n) => Math.round(Math.max(0, Math.min(1000, n)));

export function frameFit(width, height, sourceWidth, sourceHeight) {
  if ([width,height,sourceWidth,sourceHeight].some(n=>!Number.isFinite(n) || n<=0)) return null;
  const scale = Math.min(width/sourceWidth,height/sourceHeight);
  return {width:sourceWidth*scale,height:sourceHeight*scale};
}

export function framePoint(rect, x, y) {
  return [clampCoordinate((x-rect.left)*1000/rect.width),clampCoordinate((y-rect.top)*1000/rect.height)];
}

export function movePoint(points, index, point) {
  return points.map((old,i)=>i===index ? point.map(clampCoordinate) : old);
}

export function shiftContour(points, dx, dy) {
  if (!points.length) return points;
  const xs=points.map(p=>p[0]), ys=points.map(p=>p[1]);
  const sx=Math.round(Math.max(-Math.min(...xs),Math.min(1000-Math.max(...xs),dx)));
  const sy=Math.round(Math.max(-Math.min(...ys),Math.min(1000-Math.max(...ys),dy)));
  return points.map(([x,y])=>[x+sx,y+sy]);
}

function asPixels([x,y], rect) {
  return [x*rect.width/1000,y*rect.height/1000];
}

export function pickedVertex(points, point, rect, radius) {
  const p=asPixels(point,rect);
  let distance=radius, picked=-1;
  for (const [i,item] of points.entries()) {
    const a=asPixels(item,rect);
    const d=Math.hypot(a[0]-p[0],a[1]-p[1]);
    if (d<=distance) { picked=i; distance=d; }
  }
  return picked;
}

export function contourAt(points, point, rect, radius, closed) {
  if (points.length<2) return false;
  const [x,y]=asPixels(point,rect);
  let inside=false;
  for (let i=0;i<points.length-(closed ? 0 : 1);i+=1) {
    const [ax,ay]=asPixels(points[i],rect);
    const [bx,by]=asPixels(points[(i+1)%points.length],rect);
    const dx=bx-ax,dy=by-ay;
    const u=Math.min(1,Math.max(0,((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy||1)));
    if (Math.hypot(x-ax-u*dx,y-ay-u*dy)<=radius) return true;
    if (closed && (ay>y)!==(by>y) && x<(bx-ax)*(y-ay)/(by-ay)+ax) inside=!inside;
  }
  return inside;
}
